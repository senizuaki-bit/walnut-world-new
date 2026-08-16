"""Product Experience Draft GET and CAS PUT operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.game.skill_builds import (
    InvalidSkillBuildRequest,
    parse_strict_object,
    validate_source_bundle,
)
from walnut_backend.application.product.drafts import ProductDrafts

router = APIRouter()


def _drafts(request: Request) -> ProductDrafts:
    return request.app.state.product_drafts


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(
        TransportError(code, stage, message),
        get_operation_context(request),
        request.app.state.error_catalog,
    )


@router.get(
    "/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}",
    operation_id="getProductSkillDraft",
)
async def get_product_skill_draft(session_id: str, draft_id: str, request: Request) -> Any:
    result = await _drafts(request).get(session_id, draft_id, get_operation_context(request))
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/product-experience/skill-draft.schema.json",
        resource_identity={"session_id": session_id, "draft_id": draft_id},
        headers={
            "ETag": f'"draft:{result.value["revision"]}:{result.value["draft_sha256"]}"',
            "X-Draft-Revision": str(result.value["revision"]),
        },
    )


@router.put(
    "/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}",
    operation_id="upsertProductSkillDraft",
)
async def upsert_product_skill_draft(session_id: str, draft_id: str, request: Request) -> Any:
    raw_body = await request.body()
    try:
        body = parse_strict_object(raw_body)
    except InvalidSkillBuildRequest as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    schema_errors = request.app.state.contract_release.validate(
        "contracts/schemas/product-experience/skill-draft-upsert-request.schema.json", body
    )
    if schema_errors:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", schema_errors[0])
    try:
        validate_source_bundle(body)
        _validate_product_source_paths(body)
    except InvalidSkillBuildRequest as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    try:
        result = await _drafts(request).upsert(
            session_id,
            draft_id,
            raw_body,
            request.headers.get("Idempotency-Key", ""),
            get_operation_context(request),
        )
    except (InvalidSkillBuildRequest, ValueError) as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    draft = result.value.resource
    return contract_response(
        request=request,
        payload=draft,
        schema_path="contracts/schemas/product-experience/skill-draft.schema.json",
        resource_identity={"session_id": session_id, "draft_id": draft_id},
        headers={
            "Location": draft["links"]["self"],
            "ETag": f'"draft:{draft["revision"]}:{draft["draft_sha256"]}"',
            "X-Draft-Revision": str(draft["revision"]),
            "Idempotency-Replayed": "true" if result.value.replayed else "false",
        },
        status_code=result.value.http_status,
    )


def _validate_product_source_paths(body: dict[str, Any]) -> None:
    files = body["source_bundle"]["files"]
    folded_paths = [file["path"].casefold() for file in files]
    if len(set(folded_paths)) != len(folded_paths):
        raise InvalidSkillBuildRequest("source bundle paths must not collide after ASCII case folding")
