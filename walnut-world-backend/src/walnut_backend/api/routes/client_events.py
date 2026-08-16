"""Client outbox batch ingress endpoint."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.game.skill_builds import (
    InvalidSkillBuildRequest,
    parse_strict_object,
)

router = APIRouter()


@router.post("/v1/client-events:batch", operation_id="ingestClientEventBatch")
async def ingest_client_event_batch(request: Request) -> Any:
    raw_body = await request.body()
    context = get_operation_context(request)
    try:
        body = parse_strict_object(raw_body)
    except InvalidSkillBuildRequest as error:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", str(error))
    errors = request.app.state.contract_release.validate("contracts/schemas/game/client-event-batch-request.schema.json", body)
    if errors:
        return _failure(request, "INVALID_REQUEST", "VALIDATE", errors[0])
    result = await request.app.state.client_events.accept(raw_body, request.headers.get("Idempotency-Key", ""), context)
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    receipt = result.value
    command = receipt.command
    return contract_response(
        request=request,
        payload={"job_id": f"job_{hashlib.sha256(command.command_id.encode()).hexdigest()[:24]}", "job_type": "INGEST_CLIENT_EVENTS", "status": "ACCEPTED", "created_at": command.accepted_at.isoformat(), "updated_at": command.updated_at.isoformat(), "command_id": command.command_id, "trace_id": command.request_context.trace_id, "error": None},
        schema_path="contracts/schemas/game/accepted-game-job.schema.json",
        headers={"Location": f"/v1/commands/{command.command_id}", "Retry-After": "1", "Idempotency-Replayed": "false" if receipt.created else "true"},
        status_code=202,
    )


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(TransportError(code, stage, message), get_operation_context(request), request.app.state.error_catalog)
