"""Read recoverable Product session workspaces from backend-owned projections."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import canonical_payload, contract_response
from walnut_backend.application.product.workspaces import ProductWorkspaces

router = APIRouter()


@router.get(
    "/product-experience/v1/sessions/{session_id}/workspace",
    operation_id="getProductSessionWorkspace",
)
async def get_product_session_workspace(session_id: str, request: Request) -> Any:
    result = await _workspaces(request).get(session_id, get_operation_context(request))
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    payload = result.value
    session = payload.get("session")
    if not isinstance(session, Mapping) or session.get("session_id") != session_id:
        return _failure(request, "INVARIANT_VIOLATION", "RESPONSE_IDENTITY")
    etag_hash = hashlib.sha256(canonical_payload(payload)).hexdigest()
    return contract_response(
        request=request,
        payload=payload,
        schema_path="contracts/schemas/product-experience/session-workspace.schema.json",
        headers={"ETag": f'"workspace:{payload["workspace_revision"]}:{etag_hash}"'},
    )


def _workspaces(request: Request) -> ProductWorkspaces:
    return request.app.state.product_workspaces


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(
        TransportError(code, stage, message),
        get_operation_context(request),
        request.app.state.error_catalog,
    )
