"""Released asynchronous Agent Turn acceptance endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.adapters.postgres.workflow_jobs import workflow_job_id
from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.game.agent_turns import AgentTurns
from walnut_backend.application.game.skill_builds import (
    InvalidSkillBuildRequest,
    parse_strict_object,
)

router = APIRouter()


def _turns(request: Request) -> AgentTurns:
    return request.app.state.agent_turns


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(
        TransportError(code, stage, message),
        get_operation_context(request),
        request.app.state.error_catalog,
    )


@router.post("/v1/agent-sessions/{session_id}/turns", operation_id="createAgentTurn")
async def create_agent_turn(session_id: str, request: Request) -> Any:
    raw_body = await request.body()
    try:
        body = parse_strict_object(raw_body)
    except InvalidSkillBuildRequest as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    schema_errors = request.app.state.contract_release.validate(
        "contracts/schemas/game/agent-turn-create-request.schema.json", body
    )
    if schema_errors:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", schema_errors[0])
    try:
        result = await _turns(request).accept(
            session_id,
            raw_body,
            request.headers.get("Idempotency-Key", ""),
            get_operation_context(request),
        )
    except (InvalidSkillBuildRequest, ValueError) as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    receipt = result.value
    command = receipt.command
    payload = {
        "job_id": workflow_job_id(
            command.request_context.actor.tenant_id, command.command_id
        ),
        "job_type": "EXECUTE_AGENT_TURN",
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
