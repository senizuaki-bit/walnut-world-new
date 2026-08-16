"""Released Game read operationId adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.game.queries import GameQueries

router = APIRouter()
world_presentation_router = APIRouter()


def _queries(request: Request) -> GameQueries:
    return request.app.state.game_queries


def _failure_response(request: Request, result: Failure) -> Any:
    context = get_operation_context(request)
    return error_response(
        TransportError(result.error.code, result.error.stage, result.error.message),
        context,
        request.app.state.error_catalog,
    )


@router.get("/v1/bootstrap", operation_id="getGameBootstrap")
async def get_game_bootstrap(request: Request) -> Any:
    result = await _queries(request).get_bootstrap(get_operation_context(request))
    if isinstance(result, Failure):
        return _failure_response(request, result)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/bootstrap-response.schema.json",
    )


@router.get("/v1/commands/{command_id}", operation_id="getCommand")
async def get_command(command_id: str, request: Request) -> Any:
    result = await _queries(request).get_command(command_id, get_operation_context(request))
    if isinstance(result, Failure):
        return _failure_response(request, result)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/command.schema.json",
        resource_identity={"command_id": command_id},
    )


@router.get("/v1/runs/{run_id}", operation_id="getRun")
async def get_run(run_id: str, request: Request) -> Any:
    result = await _queries(request).get_run(run_id, get_operation_context(request))
    if isinstance(result, Failure):
        return _failure_response(request, result)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/run.schema.json",
        resource_identity={"run_id": run_id},
    )


@router.get("/v1/evidence/{evidence_id}", operation_id="getEvidence")
async def get_evidence(evidence_id: str, request: Request) -> Any:
    result = await _queries(request).get_evidence(evidence_id, get_operation_context(request))
    if isinstance(result, Failure):
        return _failure_response(request, result)
    reference = result.value.get("evidence_ref")
    if not isinstance(reference, dict) or reference.get("evidence_id") != evidence_id:
        return _failure_response(request, Failure(_invalid_query_error()))
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/evidence.schema.json",
        headers={"ETag": _evidence_etag(result.value)},
    )


@router.get("/v1/worlds/{world_id}/snapshot", operation_id="getWorldSnapshot")
async def get_world_snapshot(world_id: str, request: Request) -> Any:
    result = await _queries(request).get_world_snapshot(world_id, get_operation_context(request))
    if isinstance(result, Failure):
        return _failure_response(request, result)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/world-snapshot.schema.json",
        resource_identity={"world_id": world_id},
        headers={
            "ETag": f'"{world_id}:{result.value["revision"]}:{result.value["state_hash"]}"',
            "X-World-Revision": str(result.value["revision"]),
        },
    )


@router.get("/v1/worlds/{world_id}/events", operation_id="listWorldEvents")
async def list_world_events(
    world_id: str, after_sequence: int, request: Request, limit: int = 100
) -> Any:
    if not 1 <= limit <= 500 or after_sequence < 0:
        return _failure_response(
            request,
            Failure(_invalid_query_error()),
        )
    result = await _queries(request).list_world_events(
        world_id, after_sequence, limit, get_operation_context(request)
    )
    if isinstance(result, Failure):
        return _failure_response(request, result)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/world-event-page.schema.json",
        resource_identity={"world_id": world_id},
        headers={"X-World-Revision": str(result.value["snapshot_revision"])},
    )


@world_presentation_router.get(
    "/v1/worlds/{world_id}/presentation-events",
    operation_id="listWorldPresentationEvents",
)
async def list_world_presentation_events(
    world_id: str, after_sequence: int, request: Request, limit: int = 100
) -> Any:
    if not 1 <= limit <= 500 or after_sequence < 0:
        return _failure_response(request, Failure(_invalid_query_error()))
    result = await _queries(request).list_world_presentation_events(
        world_id, after_sequence, limit, get_operation_context(request)
    )
    if isinstance(result, Failure):
        return _failure_response(request, result)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/game/world-presentation-event-page.schema.json",
        resource_identity={"world_id": world_id},
        headers={"X-World-Revision": str(result.value["snapshot_revision"])},
    )


def _invalid_query_error() -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        code="INVALID_REQUEST",
        category=ErrorCategory.VALIDATION,
        retryable=False,
        user_message_key="request.invalid",
        stage="VALIDATE",
        message="after_sequence and limit are invalid",
    )


def _evidence_etag(value: Mapping[str, Any]) -> str:
    integrity = value.get("integrity")
    digest = integrity.get("payload_sha256") if isinstance(integrity, Mapping) else None
    return f'"{digest}"' if isinstance(digest, str) else '""'
