"""Product SkillDraft application and PostgreSQL authority.

The Product contracts deliberately keep this mutable authoring resource out of
the provider-neutral port package.  This backend-local boundary owns strict
request validation, revision-and-hash CAS, immutable history, and exact
idempotency receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from yaya_agent_contracts import ActorRef, canonical_json_sha256

from .application import HttpAttempt
from .codec import plain
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .product_application import ProductApplicationError, ProductReadResult
from .wire import ContractSchemaValidator

type _Connection = AsyncConnection[dict[str, object]]

_OPERATION = "upsertProductSkillDraft"
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}")
_TRACE_ID = re.compile(r"trace_[A-Za-z0-9_-]{8,96}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_CANONICAL_SOURCE_PATH = re.compile(
    r"(?=.{1,240}\Z)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?"
    r"(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*"
)
_SHA256 = re.compile(r"[a-f0-9]{64}")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_PRODUCT_WRITE_BODY_BYTES = 8 * 1024 * 1024
MAX_SKILL_SOURCE_BYTES = 1_048_576


class SkillDraftNotFoundError(LookupError):
    """The resource is absent from the authenticated actor scope."""


class SkillDraftConflictError(RuntimeError):
    """The supplied CAS or immutable identity does not match current state."""


class SkillDraftIdempotencyReuseError(RuntimeError):
    """An idempotency scope was reused with different raw request bytes."""


class SkillDraftInvalidRequestError(ValueError):
    """A validated wire request violates a semantic request invariant."""


class SkillDraftInvariantError(RuntimeError):
    """Durable SkillDraft anchors do not describe one canonical history."""


class SkillDraftDependencyError(RuntimeError):
    """PostgreSQL could not complete the requested operation."""


class SkillDraftCommitReconciliationRequired(RuntimeError):
    """The write receipt is durable after an unacknowledged commit."""

    def __init__(self, resource_url: str, original_trace_id: str) -> None:
        super().__init__("The durable SkillDraft write requires canonical GET reconciliation")
        self.resource_url = resource_url
        self.original_trace_id = original_trace_id


class ProductDraftReconciliationRequired(ProductApplicationError):
    """Contract-closed signal used by the Product HTTP adapter."""

    def __init__(
        self,
        *,
        session_id: str,
        draft_id: str,
        resource_url: str,
        original_trace_id: str,
    ) -> None:
        super().__init__(
            "DEPENDENCY_UNAVAILABLE",
            503,
            "PRODUCT_DRAFT_COMMIT",
            "The durable SkillDraft write requires canonical GET reconciliation",
            {"operation_was_durably_accepted": True},
        )
        self.session_id = session_id
        self.draft_id = draft_id
        self.resource_url = resource_url
        self.original_trace_id = original_trace_id


@dataclass(frozen=True, slots=True)
class ProductDraftWriteResult:
    status: int
    payload: Mapping[str, object]
    response_body: bytes
    headers: Mapping[str, str]
    replayed: bool


class SkillDraftRepository(Protocol):
    async def get_skill_draft(
        self,
        actor: ActorRef,
        session_id: str,
        draft_id: str,
    ) -> Mapping[str, object]: ...

    async def upsert_skill_draft(
        self,
        actor: ActorRef,
        attempt: HttpAttempt,
        session_id: str,
        draft_id: str,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> ProductDraftWriteResult: ...


def _mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SkillDraftInvariantError(f"{field_name} is not a JSON object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise SkillDraftInvariantError(f"{field_name} contains a non-string key")
    return {cast(str, key): item for key, item in source.items()}


def _request_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SkillDraftInvalidRequestError(f"{field_name} is not a JSON object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise SkillDraftInvalidRequestError(f"{field_name} contains a non-string key")
    return {cast(str, key): item for key, item in source.items()}


def _strict_json_object(raw: bytes, field_name: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number {value}")

    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SkillDraftInvalidRequestError(f"{field_name} is not strict UTF-8 JSON") from error
    return _request_mapping(value, field_name)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def _bytes(value: object, field_name: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    raise SkillDraftInvariantError(f"{field_name} is not binary")


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_SAFE_INTEGER
    ):
        raise SkillDraftInvariantError(f"{field_name} is outside its contract range")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise SkillDraftInvariantError(f"{field_name} is not text")
    return value


def _iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise SkillDraftInvariantError("PostgreSQL returned a naive timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise SkillDraftInvariantError(f"{field_name} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SkillDraftInvariantError(f"{field_name} is not a timestamp") from error
    if parsed.utcoffset() is None:
        raise SkillDraftInvariantError(f"{field_name} is not timezone-aware")
    return parsed


def _stable_actor_wire(value: object, actor: ActorRef) -> bool:
    try:
        wire = _mapping(value, "request_context.actor")
    except SkillDraftInvariantError:
        return False
    actor_type = getattr(actor.actor_type, "value", str(actor.actor_type))
    return (
        wire.get("tenant_id"),
        wire.get("actor_id"),
        wire.get("actor_type"),
    ) == (actor.tenant_id, actor.actor_id, actor_type)


def _draft_hash(payload: Mapping[str, object]) -> str:
    return canonical_json_sha256(
        {
            "session_id": payload["session_id"],
            "draft_id": payload["draft_id"],
            "skill_id": payload["skill_id"],
            "content_ref": payload["content_ref"],
            "display_name": payload["display_name"],
            "source_bundle": payload["source_bundle"],
        }
    )


def _resource_url(session_id: str, draft_id: str) -> str:
    return f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}"


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "".join(f"{len(part)}:{part}" for part in parts)
    return f"{prefix}_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}"


def _resource_headers(payload: Mapping[str, object]) -> dict[str, str]:
    revision = _integer(payload.get("revision"), "draft.revision", minimum=1)
    digest = _text(payload.get("draft_sha256"), "draft.draft_sha256")
    return {
        "Location": _resource_url(
            _text(payload.get("session_id"), "draft.session_id"),
            _text(payload.get("draft_id"), "draft.draft_id"),
        ),
        "ETag": f'"draft:{revision}:{digest}"',
        "X-Draft-Revision": str(revision),
    }


def validate_skill_source_bundle(value: object) -> None:
    """Enforce the frozen source invariants not expressible in JSON Schema."""

    bundle = _request_mapping(value, "source_bundle")
    entrypoint = bundle.get("entrypoint")
    files_value = bundle.get("files")
    if not isinstance(entrypoint, str) or _CANONICAL_SOURCE_PATH.fullmatch(entrypoint) is None:
        raise SkillDraftInvalidRequestError("source_bundle.entrypoint is not canonical")
    if not isinstance(files_value, Sequence) or isinstance(files_value, (str, bytes, bytearray)):
        raise SkillDraftInvalidRequestError("source_bundle.files count is outside 1..32")
    files = cast(Sequence[object], files_value)
    if not 1 <= len(files) <= 32:
        raise SkillDraftInvalidRequestError("source_bundle.files count is outside 1..32")
    folded_paths: set[str] = set()
    exact_entrypoints = 0
    total_bytes = 0
    for index, item in enumerate(files):
        file_value = _request_mapping(item, f"source_bundle.files[{index}]")
        path = file_value.get("path")
        content = file_value.get("content")
        content_sha256 = file_value.get("content_sha256")
        if not isinstance(path, str) or _CANONICAL_SOURCE_PATH.fullmatch(path) is None:
            raise SkillDraftInvalidRequestError(
                f"source_bundle.files[{index}].path is not canonical"
            )
        folded = path.lower()
        if folded in folded_paths:
            raise SkillDraftInvalidRequestError("source file paths collide after ASCII folding")
        folded_paths.add(folded)
        if path == entrypoint:
            exact_entrypoints += 1
        if not isinstance(content, str):
            raise SkillDraftInvalidRequestError(f"source_bundle.files[{index}].content is not text")
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise SkillDraftInvalidRequestError(
                "source content is not Unicode scalar text"
            ) from error
        total_bytes += len(encoded)
        if total_bytes > MAX_SKILL_SOURCE_BYTES:
            raise SkillDraftInvalidRequestError("source bundle exceeds 1048576 UTF-8 bytes")
        if (
            not isinstance(content_sha256, str)
            or _SHA256.fullmatch(content_sha256) is None
            or hashlib.sha256(encoded).hexdigest() != content_sha256
        ):
            raise SkillDraftInvalidRequestError("source file content_sha256 is incorrect")
    if exact_entrypoints != 1:
        raise SkillDraftInvalidRequestError("entrypoint does not identify exactly one source file")


class PostgresSkillDraftRepository:
    """PostgreSQL SkillDraft authority with immutable revision history."""

    def __init__(self, database: PostgresDatabase, validator: ContractSchemaValidator) -> None:
        self._database = database
        self._validator = validator

    async def get_skill_draft(
        self,
        actor: ActorRef,
        session_id: str,
        draft_id: str,
    ) -> Mapping[str, object]:
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                session = await self._session_authority(
                    connection,
                    actor,
                    session_id,
                    for_update=False,
                )
                history = await self._history(
                    connection,
                    actor,
                    session,
                    session_id,
                    draft_id,
                    for_update=False,
                )
                if not history:
                    raise SkillDraftNotFoundError("SkillDraft was not found")
                return dict(history[-1])
        except (SkillDraftNotFoundError, SkillDraftInvariantError):
            raise
        except psycopg.Error as error:
            raise SkillDraftDependencyError("PostgreSQL could not read SkillDraft") from error
        except Exception as error:
            raise SkillDraftInvariantError("SkillDraft read validation failed") from error

    async def upsert_skill_draft(
        self,
        actor: ActorRef,
        attempt: HttpAttempt,
        session_id: str,
        draft_id: str,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> ProductDraftWriteResult:
        request_sha256 = hashlib.sha256(raw_body).hexdigest()
        canonical_path = _resource_url(session_id, draft_id)
        wrote = False
        result: ProductDraftWriteResult | None = None
        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                session = await self._session_authority(
                    connection,
                    actor,
                    session_id,
                    for_update=True,
                )
                history = await self._history(
                    connection,
                    actor,
                    session,
                    session_id,
                    draft_id,
                    for_update=True,
                )
                result = await self._receipt(
                    connection,
                    actor,
                    session,
                    session_id,
                    draft_id,
                    canonical_path,
                    idempotency_key,
                    request_sha256,
                    raw_body,
                    history,
                    for_update=True,
                )
                if result is None:
                    payload, status = await self._next_payload(
                        connection,
                        actor,
                        attempt,
                        session,
                        session_id,
                        draft_id,
                        body,
                        history,
                    )
                    response_body = _json_bytes(payload)
                    headers = _resource_headers(payload)
                    wrote = True
                    await self._persist_revision(
                        connection,
                        actor,
                        payload,
                        history,
                    )
                    await connection.execute(
                        """
                        INSERT INTO yaya_product_write_receipts(
                            tenant_id,receipt_id,actor_id,content_hash,operation,
                            canonical_path,idempotency_key,request_sha256,request_body,
                            response_status,session_id,draft_id,skill_id,revision,
                            draft_sha256,original_trace_id,response_sha256,response_body,
                            response_headers,response_json,location,etag,created_at
                        ) VALUES (
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s,clock_timestamp()
                        )
                        """,
                        (
                            actor.tenant_id,
                            _scoped_identifier(
                                "receipt",
                                actor.tenant_id,
                                actor.actor_id,
                                _OPERATION,
                                canonical_path,
                                idempotency_key,
                            ),
                            actor.actor_id,
                            cast(Mapping[str, object], payload["content_ref"])["content_hash"],
                            _OPERATION,
                            canonical_path,
                            idempotency_key,
                            request_sha256,
                            raw_body,
                            status,
                            session_id,
                            draft_id,
                            payload["skill_id"],
                            payload["revision"],
                            payload["draft_sha256"],
                            attempt.trace_id,
                            hashlib.sha256(response_body).hexdigest(),
                            response_body,
                            Jsonb({**headers, "Idempotency-Replayed": "false"}),
                            Jsonb(payload),
                            headers["Location"],
                            headers["ETag"],
                        ),
                    )
                    result = ProductDraftWriteResult(
                        status,
                        payload,
                        response_body,
                        {**headers, "Idempotency-Replayed": "false"},
                        False,
                    )
        except PostgresCommitStateUnknown as error:
            if wrote:
                try:
                    replay = await self._lookup_after_unknown_commit(
                        actor,
                        session_id,
                        draft_id,
                        canonical_path,
                        idempotency_key,
                        request_sha256,
                        raw_body,
                    )
                except (
                    SkillDraftNotFoundError,
                    SkillDraftIdempotencyReuseError,
                    SkillDraftInvariantError,
                    SkillDraftDependencyError,
                ) as lookup_error:
                    raise SkillDraftDependencyError(
                        "PostgreSQL did not confirm the SkillDraft commit"
                    ) from lookup_error
                if replay is not None:
                    raise SkillDraftCommitReconciliationRequired(
                        replay.headers["Location"],
                        await self._receipt_original_trace(
                            actor,
                            canonical_path,
                            idempotency_key,
                        ),
                    ) from error
            raise SkillDraftDependencyError(
                "PostgreSQL did not confirm the SkillDraft transaction"
            ) from error
        except (
            SkillDraftNotFoundError,
            SkillDraftConflictError,
            SkillDraftIdempotencyReuseError,
            SkillDraftInvalidRequestError,
            SkillDraftInvariantError,
        ):
            raise
        except psycopg.Error as error:
            raise SkillDraftDependencyError("PostgreSQL could not write SkillDraft") from error
        except Exception as error:
            raise SkillDraftInvariantError("SkillDraft write validation failed") from error
        return result

    async def _lookup_after_unknown_commit(
        self,
        actor: ActorRef,
        session_id: str,
        draft_id: str,
        canonical_path: str,
        idempotency_key: str,
        request_sha256: str,
        raw_body: bytes,
    ) -> ProductDraftWriteResult | None:
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                session = await self._session_authority(
                    connection,
                    actor,
                    session_id,
                    for_update=False,
                )
                history = await self._history(
                    connection,
                    actor,
                    session,
                    session_id,
                    draft_id,
                    for_update=False,
                )
                return await self._receipt(
                    connection,
                    actor,
                    session,
                    session_id,
                    draft_id,
                    canonical_path,
                    idempotency_key,
                    request_sha256,
                    raw_body,
                    history,
                    for_update=False,
                )
        except (
            SkillDraftNotFoundError,
            SkillDraftIdempotencyReuseError,
            SkillDraftInvariantError,
        ):
            raise
        except psycopg.Error as error:
            raise SkillDraftDependencyError("PostgreSQL could not reconcile SkillDraft") from error

    async def _receipt_original_trace(
        self,
        actor: ActorRef,
        canonical_path: str,
        idempotency_key: str,
    ) -> str:
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                cursor = await connection.execute(
                    """
                    SELECT original_trace_id FROM yaya_product_write_receipts
                    WHERE tenant_id=%s AND actor_id=%s AND operation=%s
                      AND canonical_path=%s AND idempotency_key=%s
                    """,
                    (
                        actor.tenant_id,
                        actor.actor_id,
                        _OPERATION,
                        canonical_path,
                        idempotency_key,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise SkillDraftInvariantError(
                        "Durable receipt disappeared during reconciliation"
                    )
                trace_id = _text(row["original_trace_id"], "receipt.original_trace_id")
                if _TRACE_ID.fullmatch(trace_id) is None:
                    raise SkillDraftInvariantError("Receipt original trace is invalid")
                return trace_id
        except SkillDraftInvariantError:
            raise
        except psycopg.Error as error:
            raise SkillDraftDependencyError("PostgreSQL could not read write receipt") from error

    async def _session_authority(
        self,
        connection: _Connection,
        actor: ActorRef,
        session_id: str,
        *,
        for_update: bool,
    ) -> dict[str, object]:
        lock = " FOR UPDATE" if for_update else ""
        cursor = await connection.execute(
            """
            SELECT tenant_id,session_id,authority_id,actor_id,learner_id,
                   agent_profile_id,world_id,task_id,content_hash,status,resource_json,
                   resource_sha256,created_at,updated_at
            FROM yaya_public_agent_sessions
            WHERE tenant_id=%s AND session_id=%s AND actor_id=%s
            """
            + lock,
            (actor.tenant_id, session_id, actor.actor_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise SkillDraftNotFoundError("Product Session was not found")
        resource = _mapping(row["resource_json"], "public Session resource_json")
        try:
            self._validator.validate("schemas/game/agent-session.schema.json", resource)
        except (TypeError, ValueError, RuntimeError) as error:
            raise SkillDraftInvariantError("Public Session violates its frozen schema") from error
        context = _mapping(resource.get("request_context"), "public Session request_context")
        content = _mapping(resource.get("content"), "public Session content")
        context_content = _mapping(
            context.get("content_ref"),
            "public Session request_context.content_ref",
        )
        if not _stable_actor_wire(context.get("actor"), actor):
            raise SkillDraftNotFoundError("Product Session was not found")
        if (
            row["tenant_id"] != actor.tenant_id
            or row["session_id"] != session_id
            or row["actor_id"] != actor.actor_id
            or resource.get("session_id") != session_id
            or resource.get("learner_id") != row["learner_id"]
            or resource.get("agent_profile_id") != row["agent_profile_id"]
            or resource.get("world_id") != row["world_id"]
            or resource.get("status") != row["status"]
            or content != context_content
            or content.get("content_hash") != row["content_hash"]
            or row["resource_sha256"] != canonical_json_sha256(resource)
            or not isinstance(row["created_at"], datetime)
            or not isinstance(row["updated_at"], datetime)
            or row["updated_at"] < row["created_at"]
            or not isinstance(row["authority_id"], str)
            or _IDENTIFIER.fullmatch(row["authority_id"]) is None
            or not isinstance(row["task_id"], str)
            or _IDENTIFIER.fullmatch(row["task_id"]) is None
        ):
            raise SkillDraftInvariantError("Public Session durable identity drifted")
        return resource

    async def _history(
        self,
        connection: _Connection,
        actor: ActorRef,
        session: Mapping[str, object],
        session_id: str,
        draft_id: str,
        *,
        for_update: bool,
    ) -> list[dict[str, object]]:
        lock = " FOR UPDATE" if for_update else ""
        head_cursor = await connection.execute(
            """
            SELECT tenant_id,session_id,draft_id,actor_id,skill_id,content_hash,
                   current_revision,current_draft_sha256,updated_at
            FROM yaya_skill_draft_heads
            WHERE tenant_id=%s AND session_id=%s AND draft_id=%s
            """
            + lock,
            (actor.tenant_id, session_id, draft_id),
        )
        head = await head_cursor.fetchone()
        revision_cursor = await connection.execute(
            """
            SELECT tenant_id,session_id,draft_id,actor_id,skill_id,content_hash,
                   revision,draft_sha256,source_bundle_sha256,source_bundle_json,
                   resource_json,resource_sha256,created_at
            FROM yaya_skill_draft_revisions
            WHERE tenant_id=%s AND session_id=%s AND draft_id=%s
            ORDER BY revision
            """
            + lock,
            (actor.tenant_id, session_id, draft_id),
        )
        rows = list(await revision_cursor.fetchall())
        if head is None:
            if rows:
                raise SkillDraftInvariantError("SkillDraft has revisions without a head")
            return []
        if head["actor_id"] != actor.actor_id:
            raise SkillDraftNotFoundError("SkillDraft was not found")
        current_revision = _integer(
            head["current_revision"],
            "draft head current_revision",
            minimum=1,
        )
        if len(rows) != current_revision:
            raise SkillDraftInvariantError("SkillDraft revision history is not gap-free")
        session_content = _mapping(session.get("content"), "public Session content")
        resources: list[dict[str, object]] = []
        origin_context: dict[str, object] | None = None
        origin_created: object = None
        origin_identity: tuple[object, object, object, object] | None = None
        previous_updated: datetime | None = None
        for expected_revision, row in enumerate(rows, start=1):
            if _integer(row["revision"], "draft revision", minimum=1) != expected_revision:
                raise SkillDraftInvariantError("SkillDraft revision history contains a gap")
            resource = _mapping(row["resource_json"], "SkillDraft resource_json")
            self._validate_resource(
                resource,
                actor=actor,
                session_content=session_content,
                session_id=session_id,
                draft_id=draft_id,
            )
            context = _mapping(resource.get("request_context"), "SkillDraft request_context")
            identity = (
                resource.get("session_id"),
                resource.get("draft_id"),
                resource.get("skill_id"),
                resource.get("content_ref"),
            )
            if origin_context is None:
                origin_context = context
                origin_created = resource.get("created_at")
                origin_identity = identity
            elif (
                context != origin_context
                or resource.get("created_at") != origin_created
                or identity != origin_identity
            ):
                raise SkillDraftInvariantError("SkillDraft immutable origin or identity drifted")
            updated = _parse_iso(resource.get("updated_at"), "SkillDraft updated_at")
            if previous_updated is not None and updated < previous_updated:
                raise SkillDraftInvariantError("SkillDraft updated_at moved backwards")
            previous_updated = updated
            digest = _text(resource.get("draft_sha256"), "SkillDraft draft_sha256")
            source_bundle = _mapping(
                resource.get("source_bundle"),
                "SkillDraft source_bundle",
            )
            if (
                row["tenant_id"] != actor.tenant_id
                or row["session_id"] != session_id
                or row["draft_id"] != draft_id
                or row["actor_id"] != actor.actor_id
                or row["skill_id"] != resource.get("skill_id")
                or row["content_hash"] != session_content.get("content_hash")
                or row["revision"] != resource.get("revision")
                or row["draft_sha256"] != digest
                or row["source_bundle_json"] != source_bundle
                or row["source_bundle_sha256"] != canonical_json_sha256(source_bundle)
                or row["resource_sha256"] != canonical_json_sha256(resource)
                or not isinstance(row["created_at"], datetime)
                or _iso(row["created_at"]) != resource.get("updated_at")
            ):
                raise SkillDraftInvariantError("SkillDraft revision durable anchors drifted")
            resources.append(resource)
        current = resources[-1]
        if (
            head["tenant_id"] != actor.tenant_id
            or head["session_id"] != session_id
            or head["draft_id"] != draft_id
            or head["skill_id"] != current.get("skill_id")
            or head["content_hash"] != session_content.get("content_hash")
            or head["current_revision"] != current.get("revision")
            or head["current_draft_sha256"] != current.get("draft_sha256")
            or not isinstance(head["updated_at"], datetime)
            or _iso(head["updated_at"]) != current.get("updated_at")
        ):
            raise SkillDraftInvariantError("SkillDraft head drifted from current revision")
        return resources

    def _validate_resource(
        self,
        resource: Mapping[str, object],
        *,
        actor: ActorRef,
        session_content: Mapping[str, object],
        session_id: str,
        draft_id: str,
    ) -> None:
        try:
            self._validator.validate(
                "schemas/product-experience/skill-draft.schema.json",
                resource,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise SkillDraftInvariantError("SkillDraft violates its frozen schema") from error
        context = _mapping(resource.get("request_context"), "SkillDraft request_context")
        content = _mapping(resource.get("content_ref"), "SkillDraft content_ref")
        context_content = _mapping(
            context.get("content_ref"),
            "SkillDraft request_context.content_ref",
        )
        created = _parse_iso(resource.get("created_at"), "SkillDraft created_at")
        requested = _parse_iso(context.get("requested_at"), "SkillDraft requested_at")
        links = _mapping(resource.get("links"), "SkillDraft links")
        if (
            not _stable_actor_wire(context.get("actor"), actor)
            or content != context_content
            or content != session_content
            or resource.get("session_id") != session_id
            or resource.get("draft_id") != draft_id
            or resource.get("draft_sha256") != _draft_hash(resource)
            or requested > created
            or _parse_iso(resource.get("updated_at"), "SkillDraft updated_at") < created
            or links
            != {
                "self": _resource_url(session_id, draft_id),
                "session_workspace": (f"/product-experience/v1/sessions/{session_id}/workspace"),
                "builds": "/v1/skill-builds",
            }
        ):
            raise SkillDraftInvariantError("SkillDraft resource identity drifted")
        try:
            validate_skill_source_bundle(resource.get("source_bundle"))
        except SkillDraftInvalidRequestError as error:
            raise SkillDraftInvariantError("Stored SkillDraft source bundle is corrupt") from error

    async def _receipt(
        self,
        connection: _Connection,
        actor: ActorRef,
        session: Mapping[str, object],
        session_id: str,
        draft_id: str,
        canonical_path: str,
        idempotency_key: str,
        request_sha256: str,
        raw_body: bytes,
        history: Sequence[Mapping[str, object]],
        *,
        for_update: bool,
    ) -> ProductDraftWriteResult | None:
        lock = " FOR UPDATE" if for_update else ""
        cursor = await connection.execute(
            """
            SELECT tenant_id,receipt_id,actor_id,content_hash,operation,canonical_path,
                   idempotency_key,request_sha256,request_body,response_status,
                   session_id,draft_id,skill_id,revision,draft_sha256,original_trace_id,
                   response_sha256,response_body,response_headers,response_json,
                   location,etag,created_at
            FROM yaya_product_write_receipts
            WHERE tenant_id=%s AND actor_id=%s AND operation=%s
              AND canonical_path=%s AND idempotency_key=%s
            """
            + lock,
            (
                actor.tenant_id,
                actor.actor_id,
                _OPERATION,
                canonical_path,
                idempotency_key,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        stored_request = _bytes(row["request_body"], "receipt.request_body")
        stored_request_sha = _text(row["request_sha256"], "receipt.request_sha256")
        response_body = _bytes(row["response_body"], "receipt.response_body")
        response_sha = _text(row["response_sha256"], "receipt.response_sha256")
        original_trace = _text(row["original_trace_id"], "receipt.original_trace_id")
        expected_receipt_id = _scoped_identifier(
            "receipt",
            actor.tenant_id,
            actor.actor_id,
            _OPERATION,
            canonical_path,
            idempotency_key,
        )
        if (
            row["tenant_id"] != actor.tenant_id
            or row["receipt_id"] != expected_receipt_id
            or row["actor_id"] != actor.actor_id
            or row["operation"] != _OPERATION
            or row["canonical_path"] != canonical_path
            or row["idempotency_key"] != idempotency_key
            or hashlib.sha256(stored_request).hexdigest() != stored_request_sha
            or hashlib.sha256(response_body).hexdigest() != response_sha
            or _TRACE_ID.fullmatch(original_trace) is None
            or not isinstance(row["created_at"], datetime)
        ):
            raise SkillDraftInvariantError("Product write receipt anchors drifted")
        if stored_request_sha != request_sha256 or stored_request != raw_body:
            raise SkillDraftIdempotencyReuseError(
                "Idempotency-Key was reused with different request bytes"
            )
        try:
            payload = _strict_json_object(response_body, "receipt.response_body")
        except SkillDraftInvalidRequestError as error:
            raise SkillDraftInvariantError("Receipt response body is not strict JSON") from error
        session_content = _mapping(session.get("content"), "public Session content")
        self._validate_resource(
            payload,
            actor=actor,
            session_content=session_content,
            session_id=session_id,
            draft_id=draft_id,
        )
        revision = _integer(row["revision"], "receipt.revision", minimum=1)
        if revision > len(history) or dict(history[revision - 1]) != payload:
            raise SkillDraftInvariantError("Receipt response is not an immutable draft revision")
        raw_headers = _mapping(row["response_headers"], "receipt.response_headers")
        if any(not isinstance(value, str) for value in raw_headers.values()):
            raise SkillDraftInvariantError("Receipt response headers are not strings")
        headers = {key: cast(str, value) for key, value in raw_headers.items()}
        expected_headers = _resource_headers(payload)
        stored_headers = {**expected_headers, "Idempotency-Replayed": "false"}
        status = _integer(row["response_status"], "receipt.response_status", minimum=1)
        content = _mapping(payload.get("content_ref"), "receipt payload content_ref")
        if (
            headers != stored_headers
            or row["content_hash"] != content.get("content_hash")
            or row["session_id"] != session_id
            or row["draft_id"] != draft_id
            or row["skill_id"] != payload.get("skill_id")
            or row["revision"] != payload.get("revision")
            or row["draft_sha256"] != payload.get("draft_sha256")
            or row["response_json"] != payload
            or row["location"] != expected_headers["Location"]
            or row["etag"] != expected_headers["ETag"]
            or status != (201 if revision == 1 else 200)
        ):
            raise SkillDraftInvariantError("Product write receipt response identity drifted")
        return ProductDraftWriteResult(
            status,
            payload,
            response_body,
            {**expected_headers, "Idempotency-Replayed": "true"},
            True,
        )

    async def _next_payload(
        self,
        connection: _Connection,
        actor: ActorRef,
        attempt: HttpAttempt,
        session: Mapping[str, object],
        session_id: str,
        draft_id: str,
        body: Mapping[str, object],
        history: Sequence[Mapping[str, object]],
    ) -> tuple[dict[str, object], int]:
        base_revision_value = body.get("base_revision")
        if isinstance(base_revision_value, bool) or not isinstance(base_revision_value, int):
            raise SkillDraftInvalidRequestError("base_revision is not an integer")
        base_revision = base_revision_value
        base_hash = body.get("base_draft_sha256")
        if history:
            current = history[-1]
            if (
                base_revision != current.get("revision")
                or base_hash != current.get("draft_sha256")
                or body.get("skill_id") != current.get("skill_id")
                or body.get("content_ref") != current.get("content_ref")
            ):
                raise SkillDraftConflictError("SkillDraft revision, hash, or identity is stale")
            revision = base_revision + 1
            origin_context = _mapping(current.get("request_context"), "SkillDraft context")
            created_at = current.get("created_at")
            time_floor = _parse_iso(current.get("updated_at"), "SkillDraft updated_at")
            status = 200
        else:
            if base_revision != 0 or base_hash is not None:
                raise SkillDraftConflictError("SkillDraft create base does not match absence")
            revision = 1
            content = _mapping(body.get("content_ref"), "request content_ref")
            origin_context = {
                "schema_version": attempt.schema_version,
                "request_id": attempt.request_id,
                "correlation_id": attempt.correlation_id,
                "trace_id": attempt.trace_id,
                "requested_at": _iso(attempt.requested_at),
                "actor": cast(dict[str, object], plain(actor)),
                "content_ref": content,
            }
            created_at = None
            time_floor = attempt.requested_at
            status = 201
        session_content = _mapping(session.get("content"), "public Session content")
        if body.get("content_ref") != session_content:
            raise SkillDraftConflictError("SkillDraft content_ref differs from its Session")
        clock_cursor = await connection.execute("SELECT clock_timestamp() AS value")
        clock_row = await clock_cursor.fetchone()
        if clock_row is None or not isinstance(clock_row["value"], datetime):
            raise SkillDraftInvariantError("PostgreSQL clock query returned no timestamp")
        now = max(clock_row["value"], time_floor)
        if revision == 1:
            created_at = _iso(now)
        payload: dict[str, object] = {
            "request_context": origin_context,
            "session_id": session_id,
            "draft_id": draft_id,
            "skill_id": body["skill_id"],
            "revision": revision,
            "content_ref": body["content_ref"],
            "display_name": body["display_name"],
            "source_bundle": body["source_bundle"],
            "draft_sha256": "",
            "created_at": created_at,
            "updated_at": _iso(now),
            "last_applied_patch_id": None,
            "links": {
                "self": _resource_url(session_id, draft_id),
                "session_workspace": (f"/product-experience/v1/sessions/{session_id}/workspace"),
                "builds": "/v1/skill-builds",
            },
        }
        payload["draft_sha256"] = _draft_hash(payload)
        self._validate_resource(
            payload,
            actor=actor,
            session_content=session_content,
            session_id=session_id,
            draft_id=draft_id,
        )
        return payload, status

    @staticmethod
    async def _persist_revision(
        connection: _Connection,
        actor: ActorRef,
        payload: Mapping[str, object],
        history: Sequence[Mapping[str, object]],
    ) -> None:
        session_id = cast(str, payload["session_id"])
        draft_id = cast(str, payload["draft_id"])
        skill_id = cast(str, payload["skill_id"])
        content = _mapping(payload["content_ref"], "SkillDraft content_ref")
        revision = cast(int, payload["revision"])
        digest = cast(str, payload["draft_sha256"])
        updated_at = _parse_iso(payload["updated_at"], "SkillDraft updated_at")
        resource_sha = canonical_json_sha256(payload)
        source_bundle = _mapping(payload["source_bundle"], "SkillDraft source_bundle")
        await connection.execute(
            """
            INSERT INTO yaya_skill_draft_revisions(
                tenant_id,session_id,draft_id,actor_id,skill_id,content_hash,
                revision,draft_sha256,source_bundle_sha256,source_bundle_json,
                resource_json,resource_sha256,created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                actor.tenant_id,
                session_id,
                draft_id,
                actor.actor_id,
                skill_id,
                content["content_hash"],
                revision,
                digest,
                canonical_json_sha256(source_bundle),
                Jsonb(source_bundle),
                Jsonb(dict(payload)),
                resource_sha,
                updated_at,
            ),
        )
        if not history:
            await connection.execute(
                """
                INSERT INTO yaya_skill_draft_heads(
                    tenant_id,session_id,draft_id,actor_id,skill_id,content_hash,
                    current_revision,current_draft_sha256,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    actor.tenant_id,
                    session_id,
                    draft_id,
                    actor.actor_id,
                    skill_id,
                    content["content_hash"],
                    revision,
                    digest,
                    updated_at,
                ),
            )
        else:
            previous = history[-1]
            cursor = await connection.execute(
                """
                UPDATE yaya_skill_draft_heads
                SET current_revision=%s,current_draft_sha256=%s,updated_at=%s
                WHERE tenant_id=%s AND session_id=%s AND draft_id=%s AND actor_id=%s
                  AND current_revision=%s AND current_draft_sha256=%s
                """,
                (
                    revision,
                    digest,
                    updated_at,
                    actor.tenant_id,
                    session_id,
                    draft_id,
                    actor.actor_id,
                    previous["revision"],
                    previous["draft_sha256"],
                ),
            )
            if cursor.rowcount != 1:
                raise SkillDraftConflictError("SkillDraft head changed during CAS")


class ProductSkillDraftApplication:
    def __init__(
        self,
        repository: SkillDraftRepository,
        validator: ContractSchemaValidator,
    ) -> None:
        self._repository = repository
        self._validator = validator

    async def get_skill_draft(
        self,
        actor: ActorRef,
        session_id: str,
        draft_id: str,
    ) -> ProductReadResult:
        try:
            payload = dict(await self._repository.get_skill_draft(actor, session_id, draft_id))
        except SkillDraftNotFoundError as error:
            raise ProductApplicationError(
                "NOT_FOUND", 404, "PRODUCT_DRAFT_READ", "SkillDraft was not found"
            ) from error
        except SkillDraftDependencyError as error:
            raise ProductApplicationError(
                "DEPENDENCY_UNAVAILABLE",
                503,
                "PRODUCT_DRAFT_READ",
                "SkillDraft storage is unavailable",
            ) from error
        except SkillDraftInvariantError as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION", 500, "PRODUCT_DRAFT_READ", str(error)
            ) from error
        self._validate_outbound(payload, actor, session_id, draft_id)
        headers = _resource_headers(payload)
        headers.pop("Location")
        return ProductReadResult(payload, headers)

    async def upsert_skill_draft(
        self,
        actor: ActorRef,
        attempt: HttpAttempt,
        session_id: str,
        draft_id: str,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> ProductDraftWriteResult:
        if len(raw_body) > MAX_PRODUCT_WRITE_BODY_BYTES:
            raise ProductApplicationError(
                "PAYLOAD_TOO_LARGE",
                413,
                "PRODUCT_DRAFT_VALIDATE",
                "SkillDraft request body exceeds 8 MiB",
            )
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise ProductApplicationError(
                "INVALID_REQUEST",
                400,
                "PRODUCT_DRAFT_VALIDATE",
                "Idempotency-Key is invalid",
            )
        try:
            parsed = _strict_json_object(raw_body, "SkillDraft upsert body")
            supplied = _request_mapping(body, "SkillDraft upsert body")
            if parsed != supplied:
                raise SkillDraftInvalidRequestError(
                    "Parsed request differs from the raw bytes used for idempotency"
                )
            self._validator.validate(
                "schemas/product-experience/skill-draft-upsert-request.schema.json",
                supplied,
            )
            if supplied.get("session_id") != session_id or supplied.get("draft_id") != draft_id:
                raise ProductApplicationError(
                    "CONTENT_VERSION_MISMATCH",
                    409,
                    "PRODUCT_DRAFT_VALIDATE",
                    "SkillDraft path and body identity differ",
                )
            display_name = supplied.get("display_name")
            if not isinstance(display_name, str):
                raise SkillDraftInvalidRequestError("display_name is not text")
            display_name.encode("utf-8", errors="strict")
            validate_skill_source_bundle(supplied.get("source_bundle"))
        except ProductApplicationError:
            raise
        except (SkillDraftInvalidRequestError, TypeError, ValueError, RuntimeError) as error:
            raise ProductApplicationError(
                "INVALID_REQUEST",
                400,
                "PRODUCT_DRAFT_VALIDATE",
                "SkillDraft upsert body violates its frozen contract",
            ) from error
        try:
            result = await self._repository.upsert_skill_draft(
                actor,
                attempt,
                session_id,
                draft_id,
                idempotency_key,
                raw_body,
                supplied,
            )
        except SkillDraftCommitReconciliationRequired as error:
            raise ProductDraftReconciliationRequired(
                session_id=session_id,
                draft_id=draft_id,
                resource_url=error.resource_url,
                original_trace_id=error.original_trace_id,
            ) from error
        except SkillDraftInvalidRequestError as error:
            raise ProductApplicationError(
                "INVALID_REQUEST", 400, "PRODUCT_DRAFT_VALIDATE", str(error)
            ) from error
        except SkillDraftConflictError as error:
            raise ProductApplicationError(
                "CONTENT_VERSION_MISMATCH", 409, "PRODUCT_DRAFT_CAS", str(error)
            ) from error
        except SkillDraftIdempotencyReuseError as error:
            raise ProductApplicationError(
                "IDEMPOTENCY_KEY_REUSED", 409, "PRODUCT_DRAFT_IDEMPOTENCY", str(error)
            ) from error
        except SkillDraftNotFoundError as error:
            raise ProductApplicationError(
                "NOT_FOUND", 404, "PRODUCT_DRAFT_WRITE", "SkillDraft Session was not found"
            ) from error
        except SkillDraftDependencyError as error:
            raise ProductApplicationError(
                "DEPENDENCY_UNAVAILABLE",
                503,
                "PRODUCT_DRAFT_WRITE",
                "SkillDraft storage is unavailable",
            ) from error
        except SkillDraftInvariantError as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION", 500, "PRODUCT_DRAFT_WRITE", str(error)
            ) from error
        self._validate_outbound(result.payload, actor, session_id, draft_id)
        try:
            parsed_response = _strict_json_object(result.response_body, "SkillDraft response")
        except SkillDraftInvalidRequestError as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_WRITE",
                "SkillDraft receipt response is not strict JSON",
            ) from error
        if (
            parsed_response != dict(result.payload)
            or result.status not in {200, 201}
            or result.headers
            != {
                **_resource_headers(result.payload),
                "Idempotency-Replayed": "true" if result.replayed else "false",
            }
        ):
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_WRITE",
                "SkillDraft write result drifted from its durable receipt",
            )
        return result

    def _validate_outbound(
        self,
        payload: Mapping[str, object],
        actor: ActorRef,
        session_id: str,
        draft_id: str,
    ) -> None:
        try:
            self._validator.validate(
                "schemas/product-experience/skill-draft.schema.json",
                payload,
            )
            context = _mapping(payload.get("request_context"), "SkillDraft request_context")
            links = _mapping(payload.get("links"), "SkillDraft links")
            if (
                not _stable_actor_wire(context.get("actor"), actor)
                or context.get("content_ref") != payload.get("content_ref")
                or payload.get("session_id") != session_id
                or payload.get("draft_id") != draft_id
                or payload.get("draft_sha256") != _draft_hash(payload)
                or links.get("self") != _resource_url(session_id, draft_id)
            ):
                raise SkillDraftInvariantError("SkillDraft outbound identity drifted")
            validate_skill_source_bundle(payload.get("source_bundle"))
        except (TypeError, ValueError, RuntimeError) as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_READ",
                "SkillDraft violates its outbound contract",
            ) from error


__all__ = [
    "MAX_PRODUCT_WRITE_BODY_BYTES",
    "PostgresSkillDraftRepository",
    "ProductDraftReconciliationRequired",
    "ProductDraftWriteResult",
    "ProductSkillDraftApplication",
    "SkillDraftCommitReconciliationRequired",
    "SkillDraftConflictError",
    "SkillDraftDependencyError",
    "SkillDraftIdempotencyReuseError",
    "SkillDraftInvalidRequestError",
    "SkillDraftInvariantError",
    "SkillDraftNotFoundError",
    "SkillDraftRepository",
    "validate_skill_source_bundle",
]
