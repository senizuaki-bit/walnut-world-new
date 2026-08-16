"""Read-only staged INT2 capability projection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response

router = APIRouter()


@router.get(
    "/product-experience/v1/capabilities",
    operation_id="getInt2Capabilities",
)
async def get_int2_capabilities(request: Request) -> Any:
    """Report immutable minimum constraints without enabling any mutation."""

    settings = request.app.state.settings
    context = get_operation_context(request)
    bootstrap = await request.app.state.student_bootstrap_queries.get(context)
    if isinstance(bootstrap, Failure):
        return error_response(
            TransportError(
                bootstrap.error.code,
                bootstrap.error.stage,
                bootstrap.error.message,
            ),
            context,
            request.app.state.error_catalog,
        )
    payload = {
        "request_context": bootstrap.value["request_context"],
        "api_version": "1.2.0",
        "contract_version": "0.6.0",
        "world_presentation_enabled": settings.world_presentation_enabled,
        "skill_patch_enabled": settings.skill_patch_enabled,
        "skill_patch_constraints": {
            "request_mode": "EXPLICIT_UI_ACTION",
            "selection_target": "FAILED_INTERACTION",
            "agent_role": "teaching_agent",
            "scenario": "RECTIFICATION",
            "required_hint_level": 4,
            "operation": "UPSERT_FILE",
            "target": "CURRENT_ENTRYPOINT",
            "max_files": 1,
            "max_operations": 1,
            "requires_failed_evidence": True,
            "cas_required": True,
            "requires_student_confirmation": True,
            "auto_build": False,
            "auto_activate": False,
            "auto_run": False,
        },
    }
    return contract_response(
        request=request,
        payload=payload,
        schema_path=("contracts/schemas/product-experience/int2-capabilities.schema.json"),
    )


__all__ = ["router"]
