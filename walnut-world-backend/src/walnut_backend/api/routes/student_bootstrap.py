"""Public additive student bootstrap v0.4 route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.game.student_bootstrap import StudentBootstrapQueries

router = APIRouter()


def _queries(request: Request) -> StudentBootstrapQueries:
    return request.app.state.student_bootstrap_queries


@router.get("/v1/student-bootstrap", operation_id="getStudentBootstrap")
async def get_student_bootstrap(request: Request) -> Any:
    context = get_operation_context(request)
    result = await _queries(request).get(context)
    if isinstance(result, Failure):
        return error_response(
            TransportError(result.error.code, result.error.stage, result.error.message),
            context,
            request.app.state.error_catalog,
        )
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/student-bootstrap-v2.schema.json",
    )


__all__ = ["router"]
