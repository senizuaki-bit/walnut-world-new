"""Product Skill Draft persistence with canonical hash CAS and scoped replay."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_build import canonical_source_bundle_sha256
from yaya_agent_contracts import ContentRef, Failure, OperationContext, Result, Success

from walnut_backend.domain.canonical_json import canonical_payload

from .models import (
    AgentSessionRow,
    ProductDraftRevisionAssistanceRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductIdempotencyReceiptRow,
    ProductWorkspaceRow,
    request_context_data,
)
from .product_workspaces import refresh_workspace_in_session
from .skill_provenance import _validate_draft_lineage


class PostgresProductDraftStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(
        self, session_id: str, draft_id: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ProductDraftRow).where(
                    ProductDraftRow.tenant_id == context.actor.tenant_id,
                    ProductDraftRow.actor_id == context.actor.actor_id,
                    ProductDraftRow.session_id == session_id,
                    ProductDraftRow.draft_id == draft_id,
                )
            )
            owner = (
                await session.scalar(
                    select(AgentSessionRow).where(
                        AgentSessionRow.tenant_id == row.tenant_id,
                        AgentSessionRow.actor_id == row.actor_id,
                        AgentSessionRow.session_id == row.session_id,
                    )
                )
                if row is not None
                else None
            )
            immutable = (
                await session.scalar(
                    select(ProductDraftRevisionRow).where(
                        ProductDraftRevisionRow.tenant_id == row.tenant_id,
                        ProductDraftRevisionRow.actor_id == row.actor_id,
                        ProductDraftRevisionRow.session_id == row.session_id,
                        ProductDraftRevisionRow.draft_id == row.draft_id,
                        ProductDraftRevisionRow.revision == row.revision,
                        ProductDraftRevisionRow.draft_sha256 == row.draft_sha256,
                    )
                )
                if row is not None
                else None
            )
            immutable_lineage = (
                await _validate_draft_lineage(session, immutable)
                if immutable is not None
                else False
            )
        if row is None:
            return Failure(_error("NOT_FOUND", "READ", "product skill draft not found"))
        if (
            owner is None
            or immutable is None
            or immutable.draft_json != row.draft_json
            or immutable_lineage is False
            or not _draft_authority_matches(row, owner)
        ):
            return Failure(
                _error(
                    "INVARIANT_VIOLATION",
                    "READ",
                    "product skill draft durable authority drifted",
                )
            )
        return Success(row.draft_json)

    async def upsert(
        self,
        session_id: str,
        draft_id: str,
        request_body: Mapping[str, Any],
        raw_body: bytes,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[DraftWrite]:
        canonical_path = f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}"
        body_hash = hashlib.sha256(raw_body).hexdigest()
        async with self._sessions() as session, session.begin():
            receipt = await session.scalar(
                select(ProductIdempotencyReceiptRow).where(
                    ProductIdempotencyReceiptRow.tenant_id == context.actor.tenant_id,
                    ProductIdempotencyReceiptRow.actor_id == context.actor.actor_id,
                    ProductIdempotencyReceiptRow.operation == "upsertProductSkillDraft",
                    ProductIdempotencyReceiptRow.canonical_path == canonical_path,
                    ProductIdempotencyReceiptRow.idempotency_key == idempotency_key,
                )
            )
            if receipt is not None:
                if receipt.request_sha256 != body_hash:
                    return Failure(
                        _error(
                            "IDEMPOTENCY_KEY_REUSED",
                            "PRODUCT_DRAFT_COMMIT",
                            "idempotency key was reused",
                        )
                    )
                previous = await session.scalar(
                    select(ProductDraftRow).where(
                        ProductDraftRow.tenant_id == context.actor.tenant_id,
                        ProductDraftRow.actor_id == context.actor.actor_id,
                        ProductDraftRow.session_id == session_id,
                        ProductDraftRow.draft_id == receipt.resource_id,
                    )
                )
                if previous is None:
                    return Failure(
                        _error(
                            "INVARIANT_VIOLATION", "PRODUCT_DRAFT_COMMIT", "receipt has no draft"
                        )
                    )
                return Success(DraftWrite(previous.draft_json, receipt.http_status, True))

            session_row = await session.scalar(
                select(AgentSessionRow)
                .where(
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.session_id == session_id,
                )
                .with_for_update()
            )
            if session_row is None:
                return Failure(_error("NOT_FOUND", "READ", "agent session not found"))
            if request_body["session_id"] != session_id or request_body["draft_id"] != draft_id:
                return Failure(
                    _error("INVALID_REQUEST", "VALIDATE", "path and body identities differ")
                )
            if request_body["content_ref"] != session_row.session_json["content"]:
                return Failure(
                    _error(
                        "CONTENT_VERSION_MISMATCH",
                        "VALIDATE",
                        "draft content differs from session content",
                    )
                )

            current = await session.scalar(
                select(ProductDraftRow)
                .where(
                    ProductDraftRow.tenant_id == context.actor.tenant_id,
                    ProductDraftRow.session_id == session_id,
                    ProductDraftRow.draft_id == draft_id,
                )
                .with_for_update()
            )
            created_at: datetime | None
            if current is None:
                if (
                    request_body["base_revision"] != 0
                    or request_body["base_draft_sha256"] is not None
                ):
                    return Failure(
                        _error(
                            "CONTENT_VERSION_MISMATCH",
                            "VALIDATE",
                            "draft create requires revision zero",
                        )
                    )
                other_skill = await session.scalar(
                    select(ProductDraftRow).where(
                        ProductDraftRow.tenant_id == context.actor.tenant_id,
                        ProductDraftRow.session_id == session_id,
                        ProductDraftRow.skill_id == request_body["skill_id"],
                    )
                )
                if other_skill is not None:
                    return Failure(
                        _error("CONTENT_VERSION_MISMATCH", "VALIDATE", "skill already has a draft")
                    )
                revision, created_at, last_patch = 1, None, None
                origin = request_context_data(
                    replace(context, content_ref=ContentRef(**request_body["content_ref"]))
                )
                status = 201
            else:
                if (
                    request_body["skill_id"] != current.skill_id
                    or request_body["base_revision"] != current.revision
                    or request_body["base_draft_sha256"] != current.draft_sha256
                ):
                    return Failure(
                        _error(
                            "CONTENT_VERSION_MISMATCH",
                            "VALIDATE",
                            "draft revision or hash is stale",
                        )
                    )
                revision, created_at = current.revision + 1, current.created_at
                origin, last_patch = (
                    current.draft_json["request_context"],
                    current.draft_json["last_applied_patch_id"],
                )
                status = 200
            workspace = await session.scalar(
                select(ProductWorkspaceRow)
                .where(
                    ProductWorkspaceRow.tenant_id == context.actor.tenant_id,
                    ProductWorkspaceRow.actor_id == context.actor.actor_id,
                    ProductWorkspaceRow.session_id == session_id,
                )
                .with_for_update()
            )
            if workspace is None:
                raise RuntimeError("durable Session has no recoverable Product workspace")
            database_now = await session.scalar(select(func.clock_timestamp()))
            if not isinstance(database_now, datetime) or database_now.tzinfo is None:
                raise RuntimeError("PostgreSQL returned an invalid Draft timestamp")
            causal_floor = [
                database_now.astimezone(UTC),
                context.requested_at.astimezone(UTC),
                session_row.updated_at.astimezone(UTC),
                workspace.updated_at.astimezone(UTC),
            ]
            if current is not None:
                causal_floor.append(current.updated_at.astimezone(UTC))
            updated_at = max(causal_floor)
            if created_at is None:
                created_at = updated_at
            parent_revision = (
                await session.scalar(
                    select(ProductDraftRevisionRow).where(
                        ProductDraftRevisionRow.tenant_id == context.actor.tenant_id,
                        ProductDraftRevisionRow.session_id == session_id,
                        ProductDraftRevisionRow.draft_id == draft_id,
                        ProductDraftRevisionRow.revision == current.revision,
                    )
                )
                if current is not None
                else None
            )
            if current is not None and parent_revision is None:
                return Failure(
                    _error(
                        "INVARIANT_VIOLATION",
                        "PRODUCT_DRAFT_COMMIT",
                        "current Draft has no immutable revision authority",
                    )
                )
            parent_assistance = (
                await _validate_draft_lineage(session, parent_revision)
                if parent_revision is not None
                else None
            )
            if (
                parent_revision is not None
                and current is not None
                and (
                    parent_assistance is False
                    or parent_revision.draft_json != current.draft_json
                    or parent_revision.draft_sha256 != current.draft_sha256
                    or parent_revision.skill_id != current.skill_id
                    or parent_revision.actor_id != current.actor_id
                )
            ):
                return Failure(
                    _error(
                        "INVARIANT_VIOLATION",
                        "PRODUCT_DRAFT_COMMIT",
                        "current Draft lineage authority is corrupt",
                    )
                )
            draft = draft_resource(
                request_body,
                origin,
                revision,
                created_at,
                updated_at,
                last_patch,
            )
            if current is None:
                session.add(
                    ProductDraftRow(
                        tenant_id=context.actor.tenant_id,
                        actor_id=context.actor.actor_id,
                        session_id=session_id,
                        draft_id=draft_id,
                        skill_id=request_body["skill_id"],
                        revision=revision,
                        draft_sha256=draft["draft_sha256"],
                        created_at=created_at,
                        updated_at=updated_at,
                        draft_json=draft,
                    )
                )
            else:
                current.revision = revision
                current.draft_sha256 = draft["draft_sha256"]
                current.updated_at = updated_at
                current.draft_json = draft
            appended = append_draft_revision_in_session(
                session,
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                draft=draft,
                source_kind="STUDENT",
                patch_id=None,
                created_at=updated_at,
                parent_revision_row_id=(
                    parent_revision.draft_revision_row_id if parent_revision is not None else None
                ),
            )
            if parent_revision is not None:
                if parent_assistance is not None and parent_assistance is not False:
                    await session.flush()
                    session.add(
                        ProductDraftRevisionAssistanceRow(
                            draft_revision_row_id=appended.draft_revision_row_id,
                            origin_accepted_revision_row_id=(
                                parent_assistance.origin_accepted_revision_row_id
                            ),
                            patch_id=parent_assistance.patch_id,
                            patch_decision_id=parent_assistance.patch_decision_id,
                            inherited=True,
                            created_at=updated_at,
                        )
                    )
            session.add(
                ProductIdempotencyReceiptRow(
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    operation="upsertProductSkillDraft",
                    canonical_path=canonical_path,
                    idempotency_key=idempotency_key,
                    request_sha256=body_hash,
                    resource_id=draft_id,
                    http_status=status,
                    original_trace_id=origin["trace_id"],
                    created_at=updated_at,
                )
            )
            await session.flush()
            await refresh_workspace_in_session(
                session,
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                session_id=session_id,
                updated_at=updated_at,
            )
            return Success(DraftWrite(draft, status, False))


class DraftWrite:
    def __init__(self, resource: dict[str, Any], http_status: int, replayed: bool) -> None:
        self.resource = resource
        self.http_status = http_status
        self.replayed = replayed


def append_draft_revision_in_session(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    draft: Mapping[str, Any],
    source_kind: str,
    patch_id: str | None,
    created_at: datetime,
    parent_revision_row_id: int | None = None,
) -> ProductDraftRevisionRow:
    """Stage one immutable Draft revision beside the mutable current-head projection."""

    source_bundle = draft.get("source_bundle")
    if not isinstance(source_bundle, Mapping):
        raise ValueError("Draft source_bundle must be an object")
    entrypoint = source_bundle.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise ValueError("Draft entrypoint must be configured")
    if (source_kind == "STUDENT") != (patch_id is None):
        raise ValueError("Draft revision source and Patch authority differ")
    if source_kind not in {"STUDENT", "SKILL_PATCH"}:
        raise ValueError("unsupported Draft revision source")
    row = ProductDraftRevisionRow(
        parent_revision_row_id=parent_revision_row_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        session_id=_draft_text(draft, "session_id"),
        draft_id=_draft_text(draft, "draft_id"),
        skill_id=_draft_text(draft, "skill_id"),
        revision=_draft_integer(draft, "revision"),
        draft_sha256=_draft_text(draft, "draft_sha256"),
        entrypoint=entrypoint,
        source_bundle_sha256=canonical_source_bundle_sha256(source_bundle),
        source_kind=source_kind,
        patch_id=patch_id,
        created_at=created_at,
        draft_json=dict(draft),
    )
    session.add(row)
    return row


def draft_resource(
    body: Mapping[str, Any],
    origin: Mapping[str, Any],
    revision: int,
    created_at: datetime,
    updated_at: datetime,
    last_patch: str | None,
) -> dict[str, Any]:
    projection = {
        "session_id": body["session_id"],
        "draft_id": body["draft_id"],
        "skill_id": body["skill_id"],
        "content_ref": body["content_ref"],
        "display_name": body["display_name"],
        "source_bundle": body["source_bundle"],
    }
    draft_sha256 = hashlib.sha256(canonical_payload(projection)).hexdigest()
    session_id = body["session_id"]
    draft_id = body["draft_id"]
    return {
        "request_context": dict(origin),
        **projection,
        "revision": revision,
        "draft_sha256": draft_sha256,
        "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "updated_at": updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "last_applied_patch_id": last_patch,
        "links": {
            "self": f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}",
            "session_workspace": f"/product-experience/v1/sessions/{session_id}/workspace",
            "builds": "/v1/skill-builds",
        },
    }


def _draft_authority_matches(row: ProductDraftRow, owner: AgentSessionRow) -> bool:
    value = row.draft_json
    origin = value.get("request_context")
    actor = origin.get("actor") if isinstance(origin, Mapping) else None
    origin_content = origin.get("content_ref") if isinstance(origin, Mapping) else None
    projection = {
        key: value.get(key)
        for key in (
            "session_id",
            "draft_id",
            "skill_id",
            "content_ref",
            "display_name",
            "source_bundle",
        )
    }
    try:
        created_at = _draft_timestamp(value.get("created_at"))
        updated_at = _draft_timestamp(value.get("updated_at"))
        digest = hashlib.sha256(canonical_payload(projection)).hexdigest()
    except (KeyError, TypeError, ValueError):
        return False
    return (
        isinstance(actor, Mapping)
        and actor.get("tenant_id") == row.tenant_id
        and actor.get("actor_id") == row.actor_id
        and isinstance(origin_content, Mapping)
        and value.get("content_ref") == origin_content == owner.session_json.get("content")
        and value.get("session_id") == row.session_id == owner.session_id
        and value.get("draft_id") == row.draft_id
        and value.get("skill_id") == row.skill_id
        and value.get("revision") == row.revision
        and value.get("draft_sha256") == row.draft_sha256 == digest
        and created_at == row.created_at
        and updated_at == row.updated_at
    )


def _draft_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def _draft_text(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Draft {key} must be a non-empty string")
    return result


def _draft_integer(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 1:
        raise ValueError(f"Draft {key} must be a positive integer")
    return result


__all__ = [
    "DraftWrite",
    "PostgresProductDraftStore",
    "append_draft_revision_in_session",
    "draft_resource",
]


def _error(code: str, stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, False, "request.invalid"),
        "CONTENT_VERSION_MISMATCH": (ErrorCategory.VALIDATION, False, "content.version_mismatch"),
        "IDEMPOTENCY_KEY_REUSED": (
            ErrorCategory.CONCURRENCY,
            False,
            "request.idempotency_conflict",
        ),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, False, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=metadata[1],
        user_message_key=metadata[2],
        stage=stage,
        message=message,
    )
