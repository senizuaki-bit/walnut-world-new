"""Exact immutable Product ContentUnit reads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.product.content import ProductContent

router = APIRouter()


@router.get(
    "/product-experience/v1/content-units/{unit_id}/versions/{content_version}",
    operation_id="getProductContentUnit",
)
async def get_product_content_unit(
    unit_id: str, content_version: str, content_hash: str, request: Request
) -> Any:
    result = await _content(request).get(
        unit_id, content_version, content_hash, get_operation_context(request)
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    payload = result.value
    return contract_response(
        request=request,
        payload=payload,
        schema_path="contracts/schemas/product-experience/content-unit.schema.json",
        resource_identity={
            "content_ref.unit_id": unit_id,
            "content_ref.version": content_version,
            "content_ref.content_hash": content_hash,
        },
        headers={"ETag": f'"{content_hash}"'},
    )


def _content(request: Request) -> ProductContent:
    return request.app.state.product_content


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(TransportError(code, stage, message), get_operation_context(request), request.app.state.error_catalog)
