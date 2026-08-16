"""Product Experience Agent interaction projection reads."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import canonical_payload, contract_response
from walnut_backend.application.game.skill_builds import (
    InvalidSkillBuildRequest,
    parse_strict_object,
)
from walnut_backend.application.product.interactions import ProductInteractions

router = APIRouter()
# PatchDecision remains outside INT1.  Keeping its implementation on a
# separate router lets focused compatibility tests exercise the dormant
# adapter without publishing the write operation from the production app.
patch_decision_router = APIRouter()


def _interactions(request: Request) -> ProductInteractions:
    return request.app.state.product_interactions


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(
        TransportError(code, stage, message),
        get_operation_context(request),
        request.app.state.error_catalog,
    )


@router.get(
    "/product-experience/v1/sessions/{session_id}/agent-interactions",
    operation_id="listProductAgentInteractions",
)
async def list_product_agent_interactions(
    session_id: str,
    request: Request,
    after_sequence: int = 0,
    limit: int = 50,
) -> Any:
    if after_sequence < 0 or limit < 1 or limit > 100:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", "invalid interaction page bounds")
    result = await _interactions(request).list(
        session_id, after_sequence, limit, get_operation_context(request)
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    page = result.value
    return contract_response(
        request=request,
        payload=page,
        schema_path="contracts/schemas/product-experience/agent-interaction-page.schema.json",
        resource_identity={"session_id": session_id},
        headers={"X-Interaction-High-Watermark": str(page["high_watermark_sequence"])},
    )


@router.get(
    "/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}",
    operation_id="getProductAgentInteraction",
)
async def get_product_agent_interaction(
    session_id: str, interaction_id: str, request: Request
) -> Any:
    result = await _interactions(request).get(
        session_id, interaction_id, get_operation_context(request)
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    interaction = result.value
    etag_hash = hashlib.sha256(canonical_payload(interaction)).hexdigest()
    return contract_response(
        request=request,
        payload=interaction,
        schema_path="contracts/schemas/product-experience/agent-interaction.schema.json",
        resource_identity={"session_id": session_id, "interaction_id": interaction_id},
        headers={
            "ETag": f'"interaction:{interaction["interaction_revision"]}:{etag_hash}"',
            "X-Interaction-Revision": str(interaction["interaction_revision"]),
        },
    )


@patch_decision_router.post(
    "/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/patches/{patch_id}/decision",
    operation_id="recordProductPatchDecision",
)
async def record_product_patch_decision(
    session_id: str, interaction_id: str, patch_id: str, request: Request
) -> Any:
    raw_body = await request.body()
    try:
        body = parse_strict_object(raw_body)
    except InvalidSkillBuildRequest as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    schema_errors = request.app.state.contract_release.validate(
        "contracts/schemas/product-experience/patch-decision-request.schema.json", body
    )
    if schema_errors:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", schema_errors[0])
    idempotency_key = request.headers.get("Idempotency-Key", "")
    if not idempotency_key:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", "Idempotency-Key is required")
    result = await _interactions(request).decide_patch(
        session_id,
        interaction_id,
        patch_id,
        body,
        raw_body,
        idempotency_key,
        get_operation_context(request),
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    write = result.value
    receipt = write.receipt
    etag_hash = hashlib.sha256(canonical_payload(receipt)).hexdigest()
    return contract_response(
        request=request,
        payload=receipt,
        schema_path="contracts/schemas/product-experience/patch-decision-receipt.schema.json",
        resource_identity={
            "session_id": session_id,
            "interaction_id": interaction_id,
            "patch_id": patch_id,
        },
        headers={
            "Location": receipt["links"]["interaction"],
            "Idempotency-Replayed": "true" if write.replayed else "false",
            "ETag": f'"interaction:{write.interaction_revision}:{etag_hash}"',
            "X-Interaction-Revision": str(write.interaction_revision),
        },
    )
