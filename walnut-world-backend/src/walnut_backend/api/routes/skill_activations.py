"""Released certified Skill activation endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.adapters.postgres.workflow_jobs import workflow_job_id
from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.game.skill_activations import SkillActivations
from walnut_backend.application.game.skill_builds import (
    InvalidSkillBuildRequest,
    parse_strict_object,
)

router = APIRouter()


def _activations(request: Request) -> SkillActivations:
    return request.app.state.skill_activations


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(
        TransportError(code, stage, message),
        get_operation_context(request),
        request.app.state.error_catalog,
    )


@router.post(
    "/v1/skill-versions/{skill_version_id}/activations",
    operation_id="activateSkillVersion",
)
async def activate_skill_version(skill_version_id: str, request: Request) -> Any:
    raw_body = await request.body()
    try:
        body = parse_strict_object(raw_body)
    except InvalidSkillBuildRequest as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    schema_errors = request.app.state.contract_release.validate(
        "contracts/schemas/game/skill-activation-request.schema.json", body
    )
    if schema_errors:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", schema_errors[0])
    try:
        result = await _activations(request).accept(
            skill_version_id,
            raw_body,
            request.headers.get("Idempotency-Key", ""),
            get_operation_context(request),
        )
    except (InvalidSkillBuildRequest, ValueError) as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    _activation_id, receipt = result.value
    command = receipt.command
    payload = {
        "job_id": workflow_job_id(
            command.request_context.actor.tenant_id, command.command_id
        ),
        "job_type": "ACTIVATE_SKILL_VERSION",
        "status": "ACCEPTED",
        "created_at": command.accepted_at.isoformat(),
        "updated_at": command.updated_at.isoformat(),
        "command_id": command.command_id,
        "trace_id": command.request_context.trace_id,
        "error": None,
    }
    return contract_response(
        request=request,
        payload=payload,
        schema_path="contracts/schemas/game/accepted-game-job.schema.json",
        headers={
            "Location": f"/v1/commands/{command.command_id}",
            "Retry-After": "1",
            "Idempotency-Replayed": "false" if receipt.created else "true",
        },
        status_code=202,
    )


@router.get(
    "/v1/skill-activations/{activation_id}",
    operation_id="getSkillActivation",
)
async def get_skill_activation(activation_id: str, request: Request) -> Any:
    result = await _activations(request).get(
        activation_id, get_operation_context(request)
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/skill-activation.schema.json",
        resource_identity={"activation_id": activation_id},
    )


__all__ = ["router"]
