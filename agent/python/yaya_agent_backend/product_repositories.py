"""Read-only PostgreSQL authority for Product AgentInteraction projections.

This module is deliberately backend-local.  The frozen cross-package port
surface has no Product read port, so exposing one there would be a contract
change rather than an implementation of the two already-frozen HTTP reads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

import psycopg
from psycopg import AsyncConnection
from yaya_agent_contracts import (
    ActorRef,
    CommandRecord,
    CommandStatus,
    ContentRef,
    EvidenceRef,
    OperationContext,
    RequestContext,
    RuntimeEvent,
    RuntimeEventType,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    CommittedAgentTurn,
    CompileResultSnapshot,
    GameEvent,
    RunResultSnapshot,
    SessionSnapshot,
    TaskSnapshot,
)
from yaya_agent_runtime.pedagogy_policy import TeachingPhase

from .codec import decode_as, plain
from .database import PostgresDatabase
from .wire import ContractSchemaValidator

type _Connection = AsyncConnection[dict[str, object]]

_MAX_SAFE_SEQUENCE = 9_007_199_254_740_991


class ProductReadNotFoundError(LookupError):
    """A resource is absent from the authenticated Product scope."""


class ProductReadCursorError(ValueError):
    """The requested cursor is ahead of the canonical session tip."""


class ProductReadInvariantError(RuntimeError):
    """Independent durable anchors do not describe one canonical projection."""


class ProductReadDependencyError(RuntimeError):
    """PostgreSQL could not complete the read-only snapshot."""


@dataclass(frozen=True, slots=True)
class ProductInteractionSnapshot:
    interaction: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProductInteractionPageSnapshot:
    request_context: Mapping[str, object]
    session_id: str
    high_watermark_sequence: int
    interactions: tuple[ProductInteractionSnapshot, ...]


class ProductInteractionReadRepository(Protocol):
    async def list_interactions(
        self,
        actor: ActorRef,
        session_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> ProductInteractionPageSnapshot: ...

    async def get_interaction(
        self,
        actor: ActorRef,
        session_id: str,
        interaction_id: str,
    ) -> ProductInteractionSnapshot: ...


def _mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ProductReadInvariantError(f"{field_name} is not a JSON object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise ProductReadInvariantError(f"{field_name} has a non-string key")
    return {cast(str, key): item for key, item in source.items()}


def _sequence(value: object, field_name: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_SAFE_SEQUENCE
    ):
        raise ProductReadInvariantError(f"{field_name} is outside the safe sequence range")
    return value


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ProductReadInvariantError(f"{field_name} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProductReadInvariantError(f"{field_name} is not a valid timestamp") from error
    if parsed.tzinfo is None:
        raise ProductReadInvariantError(f"{field_name} has no timezone")
    return parsed


def _plain_runtime_event(value: object, field_name: str) -> RuntimeEvent:
    wire = _mapping(value, field_name)
    content_ref = _mapping(wire.get("content_ref"), f"{field_name}.content_ref")
    payload = _mapping(wire.get("payload"), f"{field_name}.payload")
    occurred_at = _parse_datetime(
        wire.get("occurred_at"),
        f"{field_name}.occurred_at",
    )
    try:
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
    except (TypeError, ValueError) as error:
        raise ProductReadInvariantError(f"{field_name} is not a RuntimeEvent") from error
    if plain(event) != wire:
        raise ProductReadInvariantError(f"{field_name} is not canonical plain JSON")
    return event


def _stable_actor(left: ActorRef, right: ActorRef) -> bool:
    return (
        left.tenant_id,
        left.actor_id,
        left.actor_type,
    ) == (
        right.tenant_id,
        right.actor_id,
        right.actor_type,
    )


def _request_context_wire(value: RequestContext) -> dict[str, object]:
    """Project an OperationContext subclass onto the frozen RequestContext wire."""

    return {
        "request_id": value.request_id,
        "correlation_id": value.correlation_id,
        "trace_id": value.trace_id,
        "requested_at": plain(value.requested_at),
        "actor": plain(value.actor),
        "content_ref": plain(value.content_ref),
        "schema_version": value.schema_version,
    }


def _evidence_wire(values: Sequence[EvidenceRef]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for evidence in values:
        item: dict[str, object] = {
            "evidence_id": evidence.evidence_id,
            "evidence_type": evidence.evidence_type.value,
            "created_at": plain(evidence.created_at),
        }
        if evidence.sha256 is not None:
            item["sha256"] = evidence.sha256
        if evidence.uri is not None:
            item["uri"] = evidence.uri
        result.append(item)
    return result


class PostgresProductInteractionReadRepository:
    """Validate Product projections against independent canonical PostgreSQL facts."""

    def __init__(
        self,
        database: PostgresDatabase,
        validator: ContractSchemaValidator,
        *,
        require_internal_root: bool = False,
    ) -> None:
        self._database = database
        self._validator = validator
        self._require_internal_root = require_internal_root

    async def list_interactions(
        self,
        actor: ActorRef,
        session_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> ProductInteractionPageSnapshot:
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                session, task = await self._session_authority(connection, actor, session_id)
                high_watermark = await self._session_high_watermark(
                    connection,
                    actor.tenant_id,
                    session_id,
                )
                if after_sequence > high_watermark:
                    raise ProductReadCursorError(
                        "after_sequence is ahead of the canonical interaction tip"
                    )
                cursor = await connection.execute(
                    """
                    SELECT tenant_id,interaction_id,actor_id,content_hash,session_id,
                           turn_id,command_id,run_id,sequence,projection_json,created_at
                    FROM yaya_agent_interactions
                    WHERE tenant_id=%s AND session_id=%s
                      AND sequence>%s AND sequence<=%s
                    ORDER BY sequence
                    LIMIT %s
                    """,
                    (
                        actor.tenant_id,
                        session_id,
                        after_sequence,
                        high_watermark,
                        limit,
                    ),
                )
                rows = list(await cursor.fetchall())
                expected_count = min(limit, high_watermark - after_sequence)
                if len(rows) != expected_count:
                    raise ProductReadInvariantError(
                        "Product interaction page omitted a durable sequence"
                    )
                validated: list[ProductInteractionSnapshot] = []
                for row in rows:
                    validated.append(
                        ProductInteractionSnapshot(
                            await self._validate_interaction(
                                connection,
                                row,
                                actor=actor,
                                session=session,
                                task=task,
                            )
                        )
                    )
                snapshots = tuple(validated)
                if snapshots:
                    expected = after_sequence + 1
                    identifiers: set[str] = set()
                    for snapshot in snapshots:
                        interaction = snapshot.interaction
                        sequence = _sequence(
                            interaction.get("sequence"), "interaction.sequence", minimum=1
                        )
                        interaction_id = interaction.get("interaction_id")
                        if (
                            sequence != expected
                            or not isinstance(interaction_id, str)
                            or interaction_id in identifiers
                        ):
                            raise ProductReadInvariantError(
                                "Product interaction page is not ordered, unique, and gap-free"
                            )
                        identifiers.add(interaction_id)
                        expected += 1
                return ProductInteractionPageSnapshot(
                    request_context=_request_context_wire(session.request_context),
                    session_id=session_id,
                    high_watermark_sequence=(high_watermark if rows else after_sequence),
                    interactions=snapshots,
                )
        except (
            ProductReadNotFoundError,
            ProductReadCursorError,
            ProductReadDependencyError,
            ProductReadInvariantError,
        ):
            raise
        except psycopg.Error as error:
            raise ProductReadDependencyError(
                "PostgreSQL could not read Product interactions"
            ) from error
        except Exception as error:
            raise ProductReadInvariantError(
                "Product interaction canonical validation failed"
            ) from error

    async def get_interaction(
        self,
        actor: ActorRef,
        session_id: str,
        interaction_id: str,
    ) -> ProductInteractionSnapshot:
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                session, task = await self._session_authority(connection, actor, session_id)
                await self._session_high_watermark(
                    connection,
                    actor.tenant_id,
                    session_id,
                )
                cursor = await connection.execute(
                    """
                    SELECT tenant_id,interaction_id,actor_id,content_hash,session_id,
                           turn_id,command_id,run_id,sequence,projection_json,created_at
                    FROM yaya_agent_interactions
                    WHERE tenant_id=%s AND session_id=%s AND interaction_id=%s
                    """,
                    (actor.tenant_id, session_id, interaction_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ProductReadNotFoundError("Product interaction was not found")
                return ProductInteractionSnapshot(
                    await self._validate_interaction(
                        connection,
                        row,
                        actor=actor,
                        session=session,
                        task=task,
                    )
                )
        except (
            ProductReadNotFoundError,
            ProductReadDependencyError,
            ProductReadInvariantError,
        ):
            raise
        except psycopg.Error as error:
            raise ProductReadDependencyError(
                "PostgreSQL could not read the Product interaction"
            ) from error
        except Exception as error:
            raise ProductReadInvariantError(
                "Product interaction canonical validation failed"
            ) from error

    async def _session_authority(
        self,
        connection: _Connection,
        actor: ActorRef,
        session_id: str,
    ) -> tuple[SessionSnapshot, TaskSnapshot]:
        cursor = await connection.execute(
            """
            SELECT actor_id,task_id,world_id,content_hash,snapshot_json
            FROM yaya_agent_sessions
            WHERE tenant_id=%s AND session_id=%s AND actor_id=%s
            """,
            (actor.tenant_id, session_id, actor.actor_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ProductReadNotFoundError("Product session was not found")
        session = decode_as(row["snapshot_json"], SessionSnapshot)
        origin = session.request_context
        if (
            session.session_id != session_id
            or session.student_id != actor.actor_id
            or row["actor_id"] != actor.actor_id
            or row["task_id"] != session.task_id
            or row["world_id"] != session.world_id
            or row["content_hash"] != origin.content_ref.content_hash
            or not _stable_actor(origin.actor, actor)
        ):
            # Actor/type mismatches are scope-hidden; structural drift after a
            # correctly scoped row was found is corruption.
            if not _stable_actor(origin.actor, actor):
                raise ProductReadNotFoundError("Product session was not found")
            raise ProductReadInvariantError("Product Session identity drifted")
        task_cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,snapshot_json
            FROM yaya_tasks
            WHERE tenant_id=%s AND task_id=%s AND actor_id=%s AND content_hash=%s
            """,
            (
                actor.tenant_id,
                session.task_id,
                actor.actor_id,
                origin.content_ref.content_hash,
            ),
        )
        task_row = await task_cursor.fetchone()
        if task_row is None:
            raise ProductReadInvariantError("Product Session has no canonical Task")
        task = decode_as(task_row["snapshot_json"], TaskSnapshot)
        if (
            task.task_id != session.task_id
            or task_row["actor_id"] != actor.actor_id
            or task_row["content_hash"] != origin.content_ref.content_hash
            or not _stable_actor(task.request_context.actor, origin.actor)
            or task.request_context.content_ref != origin.content_ref
        ):
            raise ProductReadInvariantError("Product Session Task authority drifted")
        return session, task

    @staticmethod
    async def _session_high_watermark(
        connection: _Connection,
        tenant_id: str,
        session_id: str,
    ) -> int:
        cursor = await connection.execute(
            """
            SELECT COUNT(*)::bigint AS row_count,
                   COALESCE(MIN(sequence),0)::bigint AS first_sequence,
                   COALESCE(MAX(sequence),0)::bigint AS high_watermark
            FROM yaya_agent_interactions
            WHERE tenant_id=%s AND session_id=%s
            """,
            (tenant_id, session_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ProductReadInvariantError("Product high-watermark query returned no row")
        count = _sequence(row["row_count"], "interaction row count")
        first = _sequence(row["first_sequence"], "first interaction sequence")
        high = _sequence(row["high_watermark"], "interaction high-watermark")
        if (high == 0 and (count != 0 or first != 0)) or (
            high > 0 and (first != 1 or count != high)
        ):
            raise ProductReadInvariantError("Product interaction sequence contains a durable gap")
        return high

    async def _validate_interaction(
        self,
        connection: _Connection,
        row: Mapping[str, object],
        *,
        actor: ActorRef,
        session: SessionSnapshot,
        task: TaskSnapshot,
    ) -> dict[str, object]:
        interaction = _mapping(row.get("projection_json"), "AgentInteraction projection")
        self._validator.validate(
            "schemas/product-experience/agent-interaction.schema.json",
            interaction,
        )
        interaction_id = interaction.get("interaction_id")
        turn_id = interaction.get("turn_id")
        sequence = _sequence(interaction.get("sequence"), "interaction.sequence", minimum=1)
        revision = _sequence(
            interaction.get("interaction_revision"),
            "interaction.interaction_revision",
            minimum=1,
        )
        request_context = _mapping(
            interaction.get("request_context"),
            "AgentInteraction request_context",
        )
        origin_actor = _mapping(request_context.get("actor"), "AgentInteraction actor")
        origin_content = _mapping(
            request_context.get("content_ref"),
            "AgentInteraction content_ref",
        )
        canonical_actor = cast(dict[str, object], plain(session.request_context.actor))
        canonical_content = cast(dict[str, object], plain(session.request_context.content_ref))
        if (
            row.get("tenant_id") != actor.tenant_id
            or row.get("interaction_id") != interaction_id
            or row.get("actor_id") != actor.actor_id
            or row.get("content_hash") != session.request_context.content_ref.content_hash
            or row.get("session_id") != session.session_id
            or row.get("turn_id") != turn_id
            or row.get("command_id")
            != _mapping(interaction.get("feedback"), "AgentInteraction feedback").get("command_id")
            or row.get("run_id")
            != _mapping(interaction.get("feedback"), "AgentInteraction feedback").get("run_id")
            or row.get("sequence") != sequence
            or interaction.get("session_id") != session.session_id
            or origin_actor != canonical_actor
            or origin_content != canonical_content
        ):
            raise ProductReadInvariantError("AgentInteraction row identity drifted")
        if (
            revision != 1
            or interaction.get("skill_patch") is not None
            or interaction.get("patch_decision") is not None
        ):
            raise ProductReadInvariantError(
                "Product Skill Patch and PatchDecision remain closed in this slice"
            )
        created_at = interaction.get("created_at")
        if (
            not isinstance(created_at, str)
            or interaction.get("updated_at") != created_at
            or not isinstance(row.get("created_at"), datetime)
            or _iso(cast(datetime, row["created_at"])) != created_at
        ):
            raise ProductReadInvariantError("AgentInteraction timestamps drifted")
        links = _mapping(interaction.get("links"), "AgentInteraction links")
        if (
            not isinstance(interaction_id, str)
            or not isinstance(turn_id, str)
            or links
            != {
                "self": (
                    f"/product-experience/v1/sessions/{session.session_id}/"
                    f"agent-interactions/{interaction_id}"
                ),
                "session_workspace": (
                    f"/product-experience/v1/sessions/{session.session_id}/workspace"
                ),
                "skill_draft": None,
            }
        ):
            raise ProductReadInvariantError("AgentInteraction links are not canonical")

        source = _mapping(
            interaction.get("projection_source"),
            "AgentInteraction projection_source",
        )
        source_hash = source.get("source_sha256")
        source_without_hash = dict(source)
        source_without_hash.pop("source_sha256", None)
        feedback = _mapping(interaction.get("feedback"), "AgentInteraction feedback")
        feedback_hash = canonical_json_sha256(feedback)
        feedback_summary = _mapping(
            interaction.get("feedback_event"),
            "AgentInteraction feedback_event",
        )
        if (
            source_hash != canonical_json_sha256(source_without_hash)
            or source.get("actor") != request_context.get("actor")
            or source.get("content_ref") != request_context.get("content_ref")
            or source.get("interaction_id") != interaction_id
            or source.get("session_id") != session.session_id
            or source.get("turn_id") != turn_id
            or source.get("sequence") != sequence
            or source.get("command_id") != feedback.get("command_id")
            or source.get("feedback_event_id") != feedback_summary.get("event_id")
            or source.get("feedback_sha256") != feedback_hash
            or feedback_summary.get("feedback_sha256") != feedback_hash
            or any(
                source.get(name) != interaction.get(name)
                for name in ("role", "response_type", "question", "hint_level")
            )
            or source.get("skill_patch_sha256") is not None
            or source.get("committed_at") != created_at
        ):
            raise ProductReadInvariantError("AgentInteraction projection receipt drifted")
        hint_level = interaction.get("hint_level")
        if isinstance(hint_level, int) and not isinstance(hint_level, bool):
            if hint_level > task.max_hint_level:
                raise ProductReadInvariantError("AgentInteraction exceeds the pinned Task hint cap")

        await self._validate_committed_turn(
            connection,
            row=row,
            interaction=interaction,
            actor=actor,
            session=session,
            feedback=feedback,
            feedback_summary=feedback_summary,
            source=source,
        )
        return interaction

    async def _validate_committed_turn(
        self,
        connection: _Connection,
        *,
        row: Mapping[str, object],
        interaction: Mapping[str, object],
        actor: ActorRef,
        session: SessionSnapshot,
        feedback: Mapping[str, object],
        feedback_summary: Mapping[str, object],
        source: Mapping[str, object],
    ) -> None:
        command_id = feedback.get("command_id")
        if not isinstance(command_id, str):
            raise ProductReadInvariantError("AgentInteraction command_id is invalid")
        job_cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,session_id,turn_id,event_json,
                   operation_context_json,request_body,created_at,state
            FROM yaya_command_jobs
            WHERE tenant_id=%s AND command_id=%s
            """,
            (actor.tenant_id, command_id),
        )
        job_row = await job_cursor.fetchone()
        if job_row is None:
            raise ProductReadInvariantError("AgentInteraction command job is missing")
        source_event = decode_as(job_row["event_json"], GameEvent)
        source_context = decode_as(job_row["operation_context_json"], OperationContext)
        interaction_context = _mapping(
            interaction.get("request_context"),
            "AgentInteraction request_context",
        )
        if (
            job_row["actor_id"] != actor.actor_id
            or job_row["content_hash"] != session.request_context.content_ref.content_hash
            or job_row["session_id"] != session.session_id
            or job_row["turn_id"] != interaction.get("turn_id")
            or source_event.command_id != command_id
            or source_event.session_id != session.session_id
            or source_event.turn_id != interaction.get("turn_id")
            or source_context.command_id != command_id
            or not _stable_actor(source_context.actor, actor)
            or source_context.content_ref != session.request_context.content_ref
            or _request_context_wire(source_context) != interaction_context
            or not isinstance(job_row["request_body"], bytes)
        ):
            raise ProductReadInvariantError("AgentInteraction command job authority drifted")
        command = await self._validate_command(
            connection,
            actor=actor,
            session=session,
            event_turn_id=cast(str, interaction.get("turn_id")),
            command_id=command_id,
            interaction_context=interaction_context,
            feedback=feedback,
            request_body=job_row["request_body"],
            job_state=cast(str, job_row["state"]),
        )
        turn_cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,event_id,event_sha256,record_json,committed_at
            FROM yaya_agent_turns WHERE tenant_id=%s AND actor_id=%s
              AND content_hash=%s AND record_json IS NOT NULL
              AND record_json #>> '{$fields,event,$fields,command_id}'=%s
              AND record_json #>> '{$fields,event,$fields,session_id}'=%s
              AND record_json #>> '{$fields,event,$fields,turn_id}'=%s
            """,
            (
                actor.tenant_id,
                actor.actor_id,
                session.request_context.content_ref.content_hash,
                command_id,
                session.session_id,
                interaction.get("turn_id"),
            ),
        )
        candidates = list(await turn_cursor.fetchall())
        if len(candidates) not in {1, 2} or (self._require_internal_root and len(candidates) != 2):
            raise ProductReadInvariantError(
                "AgentInteraction does not resolve one public turn and at most one internal root"
            )
        root_turn_row: Mapping[str, object] | None = None
        if len(candidates) == 1:
            turn_row = candidates[0]
        else:
            root_candidates: list[Mapping[str, object]] = []
            public_candidates: list[Mapping[str, object]] = []
            for candidate in candidates:
                candidate_record = decode_as(
                    candidate["record_json"],
                    CommittedAgentTurn,
                )
                candidate_event = candidate_record.event
                candidate_hash = canonical_json_sha256(
                    _mapping(plain(candidate_event), "candidate Agent Turn event")
                )
                candidate_seed = canonical_json_sha256(
                    {
                        "tenant_id": actor.tenant_id,
                        "event_id": candidate_event.event_id,
                        "event_sha256": candidate_hash,
                    }
                )
                if candidate_event.event_id == source_event.event_id:
                    root_candidates.append(candidate)
                if source.get("receipt_id") == f"projection_{candidate_seed[:32]}":
                    public_candidates.append(candidate)
            if (
                len(root_candidates) != 1
                or len(public_candidates) != 1
                or root_candidates[0] is public_candidates[0]
            ):
                raise ProductReadInvariantError(
                    "AgentInteraction cannot distinguish its internal root and public outcome"
                )
            root_turn_row = root_candidates[0]
            turn_row = public_candidates[0]
        committed = decode_as(turn_row["record_json"], CommittedAgentTurn)
        event = committed.event
        event_sha256 = canonical_json_sha256(_mapping(plain(event), "source Agent Turn event"))
        seed = canonical_json_sha256(
            {
                "tenant_id": actor.tenant_id,
                "event_id": event.event_id,
                "event_sha256": event_sha256,
            }
        )
        self._validate_job_source_event(
            job_row=job_row,
            source_event=source_event,
            committed_event=event,
            command=command,
        )
        committed_at = turn_row.get("committed_at")
        if (
            turn_row.get("event_id") != event.event_id
            or turn_row.get("actor_id") != actor.actor_id
            or turn_row.get("content_hash") != session.request_context.content_ref.content_hash
            or turn_row.get("event_sha256") != event_sha256
            or plain(committed.actor) != interaction_context.get("actor")
            or committed.content_ref != session.request_context.content_ref
            or event.student_id != actor.actor_id
            or event.session_id != session.session_id
            or event.task_id != session.task_id
            or event.turn_id != interaction.get("turn_id")
            or event.command_id != command_id
            or not isinstance(committed_at, datetime)
            or _iso(committed_at) != source.get("committed_at")
        ):
            raise ProductReadInvariantError("Committed Agent Turn identity or hash drifted")
        decision = committed.decision
        if decision.role in {"bug_agent", "book_agent"}:
            directive = decision.teaching_directive
            base_valid = (
                decision.source == "provider"
                and not decision.degraded
                and decision.fallback_reason is None
                and directive is not None
                and not directive.patch_eligible
                and not directive.full_solution_eligible
                and decision.draft.skill_patch is None
                and not decision.draft.requires_student_confirmation
            )
            role_valid = False
            if directive is not None and decision.role == "bug_agent":
                role_valid = (
                    event.event_type in {"run_failed", "hint_requested"}
                    and event.failure_count >= 3
                    and decision.response_type == "question"
                    and directive.phase is TeachingPhase.RECTIFICATION
                    and directive.allowed_response_types == ("question",)
                )
            elif directive is not None:
                role_valid = (
                    event.event_type == "task_completed"
                    and decision.response_type == "growth_summary"
                    and directive.phase is TeachingPhase.SUMMARIZATION
                    and directive.allowed_response_types == ("growth_summary",)
                )
            if not base_valid or not role_valid:
                raise ProductReadInvariantError(
                    "AgentInteraction exposes an invalid historical Bug/Book decision"
                )
        if root_turn_row is not None:
            self._validate_internal_root_turn(
                row=root_turn_row,
                source_event=source_event,
                source_context=source_context,
                public_record=committed,
                public_committed_at=committed_at,
            )
        expected_feedback: dict[str, object] = {
            "session_id": event.session_id,
            "turn_id": event.turn_id,
            "command_id": event.command_id,
            "run_id": feedback.get("run_id"),
            "message_key": decision.message_key,
            "message": decision.message,
            "source": decision.source,
            "degraded": decision.degraded,
            "fallback_reason": decision.fallback_reason,
            "evidence_refs": _evidence_wire(decision.evidence_refs),
            "completed_at": plain(decision.completed_at),
        }
        if (
            feedback != expected_feedback
            or interaction.get("role") != decision.role
            or interaction.get("response_type") != decision.response_type
            or interaction.get("question") != decision.draft.question
            or interaction.get("hint_level") != decision.draft.hint_level
            or source.get("role") != decision.role
            or source.get("response_type") != decision.response_type
            or source.get("question") != decision.draft.question
            or source.get("hint_level") != decision.draft.hint_level
        ):
            raise ProductReadInvariantError(
                "AgentInteraction differs from the validated Agent decision"
            )
        if (
            interaction.get("interaction_id") != f"interaction_{seed[:32]}"
            or source.get("receipt_id") != f"projection_{seed[:32]}"
            or feedback_summary.get("event_id") != f"evt_feedback_{seed[:32]}"
        ):
            raise ProductReadInvariantError("AgentInteraction deterministic identity drifted")

        await self._validate_feedback_event(
            connection,
            actor=actor,
            feedback=feedback,
            feedback_summary=feedback_summary,
            command=command,
            committed_at=committed_at,
        )
        run = await self._validate_run(
            connection,
            actor=actor,
            session=session,
            event=event,
            command=command,
            interaction_context=interaction_context,
            feedback=feedback,
        )
        await self._validate_feedback_causation(
            connection,
            actor=actor,
            session=session,
            command=command,
            feedback=feedback,
            feedback_summary=feedback_summary,
            run_wire=run,
            fallback_causation_id=source_context.causation_id or command_id,
        )
        await self._validate_evidence(
            connection,
            actor=actor,
            session=session,
            event=event,
            feedback=feedback,
            run_wire=run,
        )
        await self._validate_projection_outboxes(
            connection,
            actor=actor,
            interaction=interaction,
            feedback_summary=feedback_summary,
            feedback=feedback,
            source=source,
            seed=seed,
        )

    @staticmethod
    def _validate_internal_root_turn(
        *,
        row: Mapping[str, object],
        source_event: GameEvent,
        source_context: OperationContext,
        public_record: CommittedAgentTurn,
        public_committed_at: datetime,
    ) -> None:
        root = decode_as(row["record_json"], CommittedAgentTurn)
        committed_at = row.get("committed_at")
        event_hash = canonical_json_sha256(
            _mapping(plain(source_event), "internal root Agent Turn event")
        )
        invoke_calls = tuple(
            call for call in root.decision.tool_calls if call.name == "invoke_skill"
        )
        if len(invoke_calls) != 1:
            raise ProductReadInvariantError(
                "Internal root Agent Turn has no unique SkillInvocation receipt"
            )
        summary = invoke_calls[0].result_summary
        public_event = public_record.event
        if (
            row.get("event_id") != source_event.event_id
            or row.get("event_sha256") != event_hash
            or row.get("actor_id") != source_context.actor.actor_id
            or row.get("content_hash") != source_context.content_ref.content_hash
            or not isinstance(committed_at, datetime)
            or committed_at > public_committed_at
            or root.event != source_event
            or root.actor != source_context.actor
            or root.content_ref != source_context.content_ref
            or root.route.event_type != "run_skill_requested"
            or root.route.role != "xiaohutao"
            or root.decision.role != "xiaohutao"
            or root.decision.evidence_refs != public_record.decision.evidence_refs
            or summary.get("run_id") != public_event.run_id
            or summary.get("task_success") != (public_event.event_type == "task_completed")
            or summary.get("evidence_ids")
            != tuple(item.evidence_id for item in public_event.evidence_refs)
        ):
            raise ProductReadInvariantError(
                "Internal root Agent Turn is not closed to the public Run outcome"
            )

    def _validate_job_source_event(
        self,
        *,
        job_row: Mapping[str, object],
        source_event: GameEvent,
        committed_event: GameEvent,
        command: CommandRecord,
    ) -> None:
        if source_event.event_id == committed_event.event_id:
            if source_event != committed_event:
                raise ProductReadInvariantError("AgentInteraction command job source event drifted")
            return

        request_body = job_row.get("request_body")
        created_at = job_row.get("created_at")
        if not isinstance(request_body, bytes) or not isinstance(created_at, datetime):
            raise ProductReadInvariantError(
                "AgentInteraction derived command job authority is incomplete"
            )
        try:
            request = _mapping(
                json.loads(request_body.decode("utf-8")),
                "AgentInteraction command request",
            )
            self._validator.validate(
                "schemas/game/agent-turn-create-request.schema.json",
                request,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ProductReadInvariantError(
                "AgentInteraction command request is not canonical JSON"
            ) from error
        bindings_value = request.get("skill_bindings")
        if not isinstance(bindings_value, list):
            raise ProductReadInvariantError(
                "AgentInteraction command request has no unique Skill binding"
            )
        bindings = cast(list[object], bindings_value)
        if len(bindings) != 1:
            raise ProductReadInvariantError(
                "AgentInteraction command request has no unique Skill binding"
            )
        binding = _mapping(bindings[0], "AgentInteraction Skill binding")
        framed = "".join(
            f"{len(part)}:{part}" for part in (source_event.command_id, source_event.turn_id)
        )
        accepted_event_id = f"evt_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}"
        source_identity = (
            source_event.student_id,
            source_event.task_id,
            source_event.session_id,
            source_event.turn_id,
            source_event.command_id,
            source_event.expected_world_revision,
            source_event.skill_ref,
        )
        committed_identity = (
            committed_event.student_id,
            committed_event.task_id,
            committed_event.session_id,
            committed_event.turn_id,
            committed_event.command_id,
            committed_event.expected_world_revision,
            committed_event.skill_ref,
        )
        if (
            source_event.event_type != "run_skill_requested"
            or source_event.event_id != accepted_event_id
            or source_event.occurred_at != command.accepted_at
            or created_at != command.accepted_at
            or source_event.occurred_at > committed_event.occurred_at
            or source_identity != committed_identity
            or request.get("turn_id") != source_event.turn_id
            or request.get("expected_world_revision") != source_event.expected_world_revision
            or request.get("input") != source_event.payload
            or plain(source_event.skill_ref) != binding
        ):
            raise ProductReadInvariantError(
                "AgentInteraction derived source event is not closed to its accepted command"
            )

    async def _validate_command(
        self,
        connection: _Connection,
        *,
        actor: ActorRef,
        session: SessionSnapshot,
        event_turn_id: str,
        command_id: str,
        interaction_context: Mapping[str, object],
        feedback: Mapping[str, object],
        request_body: bytes,
        job_state: str,
    ) -> CommandRecord:
        cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,session_id,turn_id,revision,status,
                   updated_at,request_sha256,record_json
            FROM yaya_commands
            WHERE tenant_id=%s AND command_id=%s
            """,
            (actor.tenant_id, command_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ProductReadInvariantError("AgentInteraction Command is missing")
        command = decode_as(row["record_json"], CommandRecord)
        if (
            row["actor_id"] != actor.actor_id
            or row["content_hash"] != session.request_context.content_ref.content_hash
            or row["session_id"] != session.session_id
            or row["turn_id"] != event_turn_id
            or row["revision"] != command.revision
            or row["status"] != command.status.value
            or not isinstance(row["updated_at"], datetime)
            or row["updated_at"] != command.updated_at
            or command.command_id != command_id
            or command.command_type != "EXECUTE_AGENT_TURN"
            or not _stable_actor(command.request_context.actor, actor)
            or command.request_context.content_ref != session.request_context.content_ref
            or _request_context_wire(command.request_context) != interaction_context
            or command.links.get("self") != f"/v1/commands/{command_id}"
            or row["request_sha256"] != hashlib.sha256(request_body).hexdigest()
        ):
            raise ProductReadInvariantError("AgentInteraction Command authority drifted")
        if not command.terminal:
            if job_state in {"READY", "LEASED"}:
                raise ProductReadDependencyError(
                    "AgentInteraction Command is still reaching its terminal state"
                )
            raise ProductReadInvariantError(
                "AgentInteraction Command job completed without a terminal Command"
            )
        if job_state != "DONE":
            raise ProductReadInvariantError(
                "Terminal AgentInteraction Command has a nonterminal job"
            )
        feedback_evidence = cast(Sequence[object], feedback.get("evidence_refs", ()))
        if any(item not in _evidence_wire(command.evidence_refs) for item in feedback_evidence):
            raise ProductReadInvariantError(
                "AgentInteraction Evidence is absent from the terminal Command"
            )
        run_id = feedback.get("run_id")
        if run_id is None:
            if (
                command.status is not CommandStatus.APPLIED
                or command.result is None
                or command.result.get("result_type") != "NO_EFFECT"
                or "run" in command.links
            ):
                raise ProductReadInvariantError(
                    "Run-free AgentInteraction is not backed by terminal NO_EFFECT"
                )
        elif command.links.get("run") != f"/v1/runs/{run_id}":
            raise ProductReadInvariantError(
                "AgentInteraction Command does not link its canonical Run"
            )
        return command

    async def _validate_feedback_event(
        self,
        connection: _Connection,
        *,
        actor: ActorRef,
        feedback: Mapping[str, object],
        feedback_summary: Mapping[str, object],
        command: CommandRecord,
        committed_at: datetime,
    ) -> None:
        event_id = feedback_summary.get("event_id")
        cursor = await connection.execute(
            """
            SELECT stream_id,sequence,event_type,event_json,occurred_at
            FROM yaya_events WHERE tenant_id=%s AND event_id=%s
            """,
            (actor.tenant_id, event_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ProductReadInvariantError("AgentInteraction feedback event is missing")
        runtime_event = decode_as(row["event_json"], RuntimeEvent)
        event_wire = _mapping(plain(runtime_event), "feedback-ready event")
        self._validator.validate(
            "schemas/game/agent-turn-feedback-ready-event.schema.json",
            event_wire,
        )
        expected_summary = dict(event_wire)
        payload = _mapping(expected_summary.pop("payload"), "feedback-ready payload")
        expected_summary["feedback_sha256"] = canonical_json_sha256(payload)
        if (
            row["stream_id"] != runtime_event.stream_id
            or row["sequence"] != runtime_event.sequence
            or row["event_type"] != runtime_event.event_type
            or not isinstance(row["occurred_at"], datetime)
            or row["occurred_at"] != runtime_event.occurred_at
            or event_wire.get("event_id") != event_id
            or payload != feedback
            or expected_summary != feedback_summary
            or runtime_event.command_id != command.command_id
            or runtime_event.stream_id != f"agent-session:{feedback.get('session_id')}"
            or runtime_event.occurred_at
            != _parse_datetime(feedback.get("completed_at"), "feedback.completed_at")
            or runtime_event.occurred_at > committed_at
            or runtime_event.trace_id != command.request_context.trace_id
            or runtime_event.correlation_id != command.request_context.correlation_id
            or runtime_event.content_ref != command.request_context.content_ref
        ):
            raise ProductReadInvariantError("AgentInteraction feedback event drifted")

    async def _validate_run(
        self,
        connection: _Connection,
        *,
        actor: ActorRef,
        session: SessionSnapshot,
        event: object,
        command: CommandRecord,
        interaction_context: Mapping[str, object],
        feedback: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        if not isinstance(event, GameEvent):
            raise ProductReadInvariantError("Committed Agent Turn event has invalid type")
        cursor = await connection.execute(
            """
            SELECT actor_id,content_hash,run_id,session_id,turn_id,command_id,
                   world_id,skill_version_id,failure_key,task_success,
                   snapshot_json,wire_json
            FROM yaya_runs
            WHERE tenant_id=%s AND session_id=%s AND turn_id=%s
            """,
            (actor.tenant_id, session.session_id, event.turn_id),
        )
        rows = list(await cursor.fetchall())
        run_id = feedback.get("run_id")
        if run_id is None:
            if rows:
                raise ProductReadInvariantError(
                    "Run-free AgentInteraction has a durable Run for the same turn"
                )
            if "run" in command.links:
                raise ProductReadInvariantError("Run-free Command links a fabricated Run")
            return None
        if len(rows) != 1:
            raise ProductReadInvariantError(
                "Run-backed AgentInteraction does not resolve exactly one Run"
            )
        row = rows[0]
        run = decode_as(row["snapshot_json"], RunResultSnapshot)
        run_wire = _mapping(row["wire_json"], "Run wire")
        self._validator.validate("schemas/game/run.schema.json", run_wire)
        if (
            row["actor_id"] != actor.actor_id
            or row["content_hash"] != session.request_context.content_ref.content_hash
            or row["run_id"] != run_id
            or row["session_id"] != session.session_id
            or row["turn_id"] != event.turn_id
            or row["command_id"] != event.command_id
            or row["world_id"] != session.world_id
            or event.skill_ref is None
            or row["skill_version_id"] != event.skill_ref.skill_version_id
            or row["failure_key"] != run.failure_key
            or row["task_success"] != run.task_success
            or run.run_id != run_id
            or run.session_id != session.session_id
            or run.turn_id != event.turn_id
            or run.command_id != event.command_id
            or run.world_id != session.world_id
            or run.skill_ref != event.skill_ref
            or (event.run_id is not None and event.run_id != run_id)
            or not _stable_actor(run.request_context.actor, actor)
            or run.request_context.content_ref != session.request_context.content_ref
            or _request_context_wire(run.request_context) != interaction_context
            or run_wire.get("run_id") != run_id
            or run_wire.get("session_id") != session.session_id
            or run_wire.get("turn_id") != event.turn_id
            or run_wire.get("command_id") != event.command_id
            or run_wire.get("request_context") != interaction_context
            or run_wire.get("skill") != plain(run.skill_ref)
            or run_wire.get("agent_feedback") != feedback
            or run_wire.get("evidence_refs") != feedback.get("evidence_refs")
            or _evidence_wire(run.evidence_refs) != feedback.get("evidence_refs")
            or run_wire.get("terminal") is not True
        ):
            raise ProductReadInvariantError("AgentInteraction Run authority drifted")
        if event.event_type == "task_completed" and not run.task_success:
            raise ProductReadInvariantError("task_completed references an unsuccessful Run")
        if event.event_type == "run_failed" and (
            run.task_success or event.failure_key is None or event.failure_key != run.failure_key
        ):
            raise ProductReadInvariantError("run_failed differs from its canonical Run outcome")
        if (
            event.event_type == "hint_requested"
            and event.failure_count >= 3
            and (
                run.task_success
                or event.failure_key is None
                or event.failure_key != run.failure_key
            )
        ):
            raise ProductReadInvariantError("Bug hint differs from its canonical failed Run")
        world_application = _mapping(
            run_wire.get("world_application"),
            "Run world_application",
        )
        if run.world_commit is None:
            if world_application.get("receipt") is not None:
                raise ProductReadInvariantError("Run wire fabricates a World receipt")
        elif world_application.get("status") != "COMMITTED" or world_application.get(
            "receipt"
        ) != plain(run.world_commit):
            raise ProductReadInvariantError("Run wire World receipt drifted from snapshot")
        if command.links.get("run") != f"/v1/runs/{run_id}":
            raise ProductReadInvariantError("Terminal Command does not link its Run")
        run_status = run_wire.get("status")
        if not isinstance(run_status, str):
            raise ProductReadInvariantError("Canonical Run status has invalid type")
        expected_command_status = {
            "SUCCEEDED": CommandStatus.APPLIED,
            "REJECTED": CommandStatus.REJECTED,
            "FAILED": CommandStatus.FAILED,
            "UNKNOWN": CommandStatus.UNKNOWN,
        }.get(run_status)
        if expected_command_status is None or command.status is not expected_command_status:
            raise ProductReadInvariantError(
                "Terminal Command status disagrees with its canonical Run"
            )
        if run_status == "SUCCEEDED":
            if run.world_commit is None or command.result is None:
                raise ProductReadInvariantError(
                    "Succeeded Run is not backed by a committed Command result"
                )
            expected_result: dict[str, object] = {
                "result_type": "WORLD_COMMIT",
                "world_id": run.world_commit.world_id,
                "previous_revision": run.world_commit.previous_revision,
                "world_revision": run.world_commit.world_revision,
                "first_event_sequence": run.world_commit.first_event_sequence,
                "last_event_sequence": run.world_commit.last_event_sequence,
            }
            if (
                dict(command.result) != expected_result
                or command.links.get("world_snapshot") != f"/v1/worlds/{session.world_id}/snapshot"
            ):
                raise ProductReadInvariantError(
                    "Terminal Command result drifted from the Run World receipt"
                )
        elif command.result is not None:
            raise ProductReadInvariantError(
                "Unsuccessful Run is backed by a fabricated Command result"
            )
        return run_wire

    async def _validate_feedback_causation(
        self,
        connection: _Connection,
        *,
        actor: ActorRef,
        session: SessionSnapshot,
        command: CommandRecord,
        feedback: Mapping[str, object],
        feedback_summary: Mapping[str, object],
        run_wire: Mapping[str, object] | None,
        fallback_causation_id: str,
    ) -> None:
        if run_wire is None:
            if feedback_summary.get("causation_id") != fallback_causation_id:
                raise ProductReadInvariantError(
                    "AgentInteraction feedback causation drifted from its accepted context"
                )
            return

        world_application = _mapping(
            run_wire.get("world_application"),
            "Run world_application",
        )
        if world_application.get("status") != "COMMITTED":
            if feedback_summary.get("causation_id") != fallback_causation_id:
                raise ProductReadInvariantError(
                    "AgentInteraction feedback causation drifted from its accepted context"
                )
            return

        receipt = _mapping(
            world_application.get("receipt"),
            "Run world receipt",
        )
        causation_id = feedback_summary.get("causation_id")
        cursor = await connection.execute(
            """
            SELECT event_id,stream_id,sequence,event_type,event_json,occurred_at
            FROM yaya_events WHERE tenant_id=%s AND event_id=%s
            """,
            (actor.tenant_id, causation_id),
        )
        rows = list(await cursor.fetchall())
        if len(rows) != 1:
            raise ProductReadInvariantError(
                "Committed Run feedback causation does not resolve exactly one World event"
            )
        row = rows[0]
        cause = _plain_runtime_event(
            row["event_json"],
            "Committed Run feedback causation",
        )
        payload = cause.payload
        first_sequence = _sequence(
            receipt.get("first_event_sequence"),
            "Run receipt first_event_sequence",
            minimum=1,
        )
        last_sequence = _sequence(
            receipt.get("last_event_sequence"),
            "Run receipt last_event_sequence",
            minimum=1,
        )
        if (
            receipt.get("world_id") != session.world_id
            or command.links.get("world_snapshot") != f"/v1/worlds/{session.world_id}/snapshot"
            or row["event_id"] != cause.event_id
            or row["stream_id"] != cause.stream_id
            or row["sequence"] != cause.sequence
            or row["event_type"] != cause.event_type
            or not isinstance(row["occurred_at"], datetime)
            or row["occurred_at"] != cause.occurred_at
            or cause.event_id != causation_id
            or cause.event_type is not RuntimeEventType.WORLD_COMMITTED
            or cause.command_id != command.command_id
            or cause.causation_id != command.command_id
            or cause.stream_id != f"world:{session.world_id}"
            or not first_sequence <= cause.sequence <= last_sequence
            or cause.trace_id != feedback_summary.get("trace_id")
            or cause.correlation_id != feedback_summary.get("correlation_id")
            or cause.content_ref != session.request_context.content_ref
            or payload.get("run_id") != run_wire.get("run_id")
            or payload.get("run_id") != feedback.get("run_id")
            or payload.get("world_id") != receipt.get("world_id")
            or payload.get("previous_world_revision") != receipt.get("previous_revision")
            or payload.get("world_revision") != receipt.get("world_revision")
            or payload.get("state_hash") != receipt.get("state_hash")
            or payload.get("committed_at") != receipt.get("committed_at")
            or payload.get("committed_at") != plain(cause.occurred_at)
            or cause.occurred_at
            > _parse_datetime(
                feedback_summary.get("occurred_at"),
                "feedback_event.occurred_at",
            )
        ):
            raise ProductReadInvariantError(
                "AgentInteraction feedback causation is outside its committed Run"
            )

    async def _validate_evidence(
        self,
        connection: _Connection,
        *,
        actor: ActorRef,
        session: SessionSnapshot,
        event: object,
        feedback: Mapping[str, object],
        run_wire: Mapping[str, object] | None,
    ) -> None:
        if not isinstance(event, GameEvent):
            raise ProductReadInvariantError("Committed Agent Turn event has invalid type")
        raw_references = feedback.get("evidence_refs")
        if isinstance(raw_references, (str, bytes, bytearray)) or not isinstance(
            raw_references, Sequence
        ):
            raise ProductReadInvariantError("AgentInteraction Evidence is not a sequence")
        references = cast(Sequence[object], raw_references)
        evidence_ids = [
            _mapping(item, "AgentInteraction EvidenceRef").get("evidence_id") for item in references
        ]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ProductReadInvariantError(
                "AgentInteraction EvidenceRef identifiers are not unique"
            )
        if run_wire is None and references:
            if event.event_type != "compile_failed" or event.build_id is None:
                raise ProductReadInvariantError(
                    "Run-free AgentInteraction cites Evidence outside a failed compile"
                )
            compile_cursor = await connection.execute(
                """
                SELECT actor_id,content_hash,snapshot_json
                FROM yaya_compile_results
                WHERE tenant_id=%s AND build_id=%s
                """,
                (actor.tenant_id, event.build_id),
            )
            compile_row = await compile_cursor.fetchone()
            if compile_row is None:
                raise ProductReadInvariantError("Run-free compile Evidence has no result")
            compile_result = decode_as(
                compile_row["snapshot_json"],
                CompileResultSnapshot,
            )
            if (
                compile_row["actor_id"] != actor.actor_id
                or compile_row["content_hash"] != session.request_context.content_ref.content_hash
                or compile_result.succeeded
                or compile_result.build_id != event.build_id
                or compile_result.skill_ref != event.skill_ref
                or not _stable_actor(compile_result.request_context.actor, actor)
                or compile_result.request_context.content_ref != session.request_context.content_ref
                or compile_result.evidence_refs != event.evidence_refs
                or _evidence_wire(compile_result.evidence_refs) != list(references)
            ):
                raise ProductReadInvariantError("Compile Evidence authority drifted")
        for raw_reference in references:
            reference = _mapping(raw_reference, "AgentInteraction EvidenceRef")
            evidence_id = reference.get("evidence_id")
            cursor = await connection.execute(
                """
                SELECT actor_id,content_hash,evidence_type,payload_sha256,
                       evidence_json,recorded_at
                FROM yaya_evidence
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (actor.tenant_id, evidence_id),
            )
            rows = list(await cursor.fetchall())
            if len(rows) != 1:
                raise ProductReadInvariantError(
                    "AgentInteraction Evidence does not resolve exactly once"
                )
            row = rows[0]
            evidence = _mapping(row["evidence_json"], "Evidence wire")
            self._validator.validate("schemas/game/evidence.schema.json", evidence)
            evidence_ref = _mapping(evidence.get("evidence_ref"), "Evidence reference")
            origin = _mapping(evidence.get("request_context"), "Evidence request_context")
            origin_actor = _mapping(origin.get("actor"), "Evidence actor")
            origin_content = _mapping(origin.get("content_ref"), "Evidence content_ref")
            integrity = _mapping(evidence.get("integrity"), "Evidence integrity")
            payload = _mapping(evidence.get("payload"), "Evidence payload")
            subject = _mapping(evidence.get("subject"), "Evidence subject")
            source = _mapping(evidence.get("source"), "Evidence source")
            if (
                row["actor_id"] != actor.actor_id
                or row["content_hash"] != session.request_context.content_ref.content_hash
                or row["evidence_type"] != reference.get("evidence_type")
                or row["payload_sha256"] != reference.get("sha256")
                or evidence_ref != reference
                or origin_actor.get("tenant_id") != actor.tenant_id
                or origin_actor.get("actor_id") != actor.actor_id
                or origin_actor.get("actor_type") != actor.actor_type.value
                or origin_content != plain(session.request_context.content_ref)
                or subject.get("learner_id") != session.student_id
                or integrity.get("payload_sha256") != row["payload_sha256"]
                or canonical_json_sha256(payload) != row["payload_sha256"]
                or not isinstance(row["recorded_at"], datetime)
                or evidence.get("recorded_at") != _iso(row["recorded_at"])
                or evidence_ref.get("created_at") != evidence.get("occurred_at")
                or _parse_datetime(evidence.get("occurred_at"), "Evidence occurred_at")
                > _parse_datetime(evidence.get("recorded_at"), "Evidence recorded_at")
                or (
                    source.get("command_id") is not None
                    and source.get("command_id") != feedback.get("command_id")
                )
                or (
                    source.get("world_id") is not None
                    and source.get("world_id") != session.world_id
                )
            ):
                raise ProductReadInvariantError("AgentInteraction Evidence authority drifted")
            evidence_kind = payload.get("evidence_kind")
            if run_wire is None and evidence_kind in {"WORLD_COMMIT", "SKILL_RUN"}:
                raise ProductReadInvariantError("Run-free feedback cites Run-scoped Evidence")
            if run_wire is not None and evidence_kind == "SKILL_RUN":
                sandbox = _mapping(run_wire.get("sandbox"), "Run sandbox")
                world = _mapping(run_wire.get("world_application"), "Run world application")
                intents = sandbox.get("action_intents")
                if not isinstance(intents, Sequence) or isinstance(
                    intents, (str, bytes, bytearray)
                ):
                    raise ProductReadInvariantError("Run action intents are invalid")
                if (
                    source.get("source_type") != "SKILL_RUN"
                    or source.get("source_id") != run_wire.get("run_id")
                    or source.get("command_id") != feedback.get("command_id")
                    or payload.get("run_id") != run_wire.get("run_id")
                    or payload.get("sandbox_status") != sandbox.get("status")
                    or payload.get("world_status") != world.get("status")
                    or payload.get("intent_count") != len(cast(Sequence[object], intents))
                    or (
                        payload.get("world_status") == "COMMITTED"
                        and source.get("world_id") != session.world_id
                    )
                ):
                    raise ProductReadInvariantError("SKILL_RUN Evidence drifted from its Run")
            if run_wire is not None and evidence_kind == "WORLD_COMMIT":
                world = _mapping(run_wire.get("world_application"), "Run world application")
                receipt = _mapping(world.get("receipt"), "Run world receipt")
                previous_revision = payload.get("previous_revision")
                world_revision = payload.get("world_revision")
                first_sequence = payload.get("first_event_sequence")
                last_sequence = payload.get("last_event_sequence")
                if (
                    source.get("source_type") != "WORLD"
                    or source.get("source_id") != session.world_id
                    or source.get("world_id") != session.world_id
                    or source.get("command_id") != feedback.get("command_id")
                    or isinstance(previous_revision, bool)
                    or not isinstance(previous_revision, int)
                    or isinstance(world_revision, bool)
                    or not isinstance(world_revision, int)
                    or world_revision != previous_revision + 1
                    or isinstance(first_sequence, bool)
                    or not isinstance(first_sequence, int)
                    or isinstance(last_sequence, bool)
                    or not isinstance(last_sequence, int)
                    or first_sequence > last_sequence
                    or any(
                        payload.get(name) != receipt.get(name)
                        for name in (
                            "world_id",
                            "previous_revision",
                            "world_revision",
                            "first_event_sequence",
                            "last_event_sequence",
                            "state_hash",
                        )
                    )
                ):
                    raise ProductReadInvariantError("WORLD_COMMIT Evidence drifted from its Run")
            if evidence_kind == "BUILD_CERTIFICATION":
                if source.get("source_type") != "SKILL_BUILD" or source.get(
                    "source_id"
                ) != payload.get("build_id"):
                    raise ProductReadInvariantError(
                        "Build Evidence drifted from its canonical source"
                    )

    async def _validate_projection_outboxes(
        self,
        connection: _Connection,
        *,
        actor: ActorRef,
        interaction: Mapping[str, object],
        feedback_summary: Mapping[str, object],
        feedback: Mapping[str, object],
        source: Mapping[str, object],
        seed: str,
    ) -> None:
        receipt_id = source.get("receipt_id")
        cursor = await connection.execute(
            """
            SELECT message_id,idempotency_key,payload_sha256,payload_json
            FROM yaya_projection_outbox
            WHERE tenant_id=%s AND destination='product_agent_interactions'
              AND payload_json #>> '{projection_source,receipt_id}'=%s
            """,
            (actor.tenant_id, receipt_id),
        )
        rows = list(await cursor.fetchall())
        if len(rows) != 1:
            raise ProductReadInvariantError(
                "projection_source receipt_id does not resolve exactly once"
            )
        row = rows[0]
        if (
            row["message_id"] != f"projection_msg_{seed[:32]}"
            or row["idempotency_key"] != f"agent-turn-product:{seed}"
            or row["payload_json"] != interaction
            or row["payload_sha256"] != canonical_json_sha256(interaction)
            or _mapping(
                _mapping(row["payload_json"], "Product projection outbox").get("projection_source"),
                "Product projection receipt",
            )
            != source
        ):
            raise ProductReadInvariantError("Product projection outbox drifted")

        event_id = feedback_summary.get("event_id")
        event_cursor = await connection.execute(
            """
            SELECT message_id,idempotency_key,payload_sha256,payload_json
            FROM yaya_projection_outbox
            WHERE tenant_id=%s AND destination='agent_feedback_events'
              AND payload_json ->> 'event_id'=%s
            """,
            (actor.tenant_id, event_id),
        )
        event_rows = list(await event_cursor.fetchall())
        if len(event_rows) != 1:
            raise ProductReadInvariantError(
                "feedback-ready outbox event does not resolve exactly once"
            )
        event_row = event_rows[0]
        event_payload = _mapping(event_row["payload_json"], "feedback-ready outbox")
        expected_event = dict(feedback_summary)
        expected_event.pop("feedback_sha256", None)
        expected_event["payload"] = dict(feedback)
        if (
            event_row["message_id"] != f"feedback_msg_{seed[:32]}"
            or event_row["idempotency_key"] != f"agent-feedback-event:{seed}"
            or event_payload != expected_event
            or event_row["payload_sha256"] != canonical_json_sha256(event_payload)
        ):
            raise ProductReadInvariantError("feedback-ready projection outbox drifted")


__all__ = [
    "PostgresProductInteractionReadRepository",
    "ProductInteractionPageSnapshot",
    "ProductInteractionReadRepository",
    "ProductInteractionSnapshot",
    "ProductReadCursorError",
    "ProductReadDependencyError",
    "ProductReadInvariantError",
    "ProductReadNotFoundError",
]
