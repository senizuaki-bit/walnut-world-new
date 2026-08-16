"""Locked Feishu read operations for teacher learning insights."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from yaya_agent_contracts import Failure

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import contract_response
from walnut_backend.application.feishu.learning_queries import FeishuLearningQueries
from walnut_backend.application.game.skill_builds import (
    InvalidSkillBuildRequest,
    parse_strict_object,
)

router = APIRouter()


def _queries(request: Request) -> FeishuLearningQueries:
    return request.app.state.feishu_learning_queries


def _failure(request: Request, code: str, stage: str, message: str | None = None) -> Any:
    return error_response(
        TransportError(code, stage, message),
        get_operation_context(request),
        request.app.state.error_catalog,
    )


async def _invalid_body_response(
    request: Request,
    *,
    operation: str,
    validation_stage: Literal["JSON", "SCHEMA"],
    message: str,
) -> Any:
    audited = await _queries(request).audit_transport_validation_failure(
        context=get_operation_context(request),
        operation=operation,
        validation_stage=validation_stage,
    )
    if isinstance(audited, Failure):
        return _failure(
            request,
            audited.error.code,
            audited.error.stage,
            audited.error.message,
        )
    return _failure(request, "INVALID_REQUEST", "VALIDATE", message)


async def _body(
    request: Request, schema_path: str, *, operation: str
) -> dict[str, Any] | Any:
    raw_body = await request.body()
    try:
        body = parse_strict_object(raw_body)
    except InvalidSkillBuildRequest as error:
        return await _invalid_body_response(
            request,
            operation=operation,
            validation_stage="JSON",
            message=str(error),
        )
    schema_errors = request.app.state.contract_release.validate(schema_path, body)
    if schema_errors:
        return await _invalid_body_response(
            request,
            operation=operation,
            validation_stage="SCHEMA",
            message=schema_errors[0],
        )
    return body


@router.post(
    "/integrations/feishu/v1/learner-queries",
    operation_id="queryLearnerProjectionFromFeishu",
)
async def query_feishu_learner(request: Request) -> Any:
    body = await _body(
        request,
        "contracts/schemas/feishu/learner-query.schema.json",
        operation="FEISHU_LEARNER_QUERY",
    )
    if not isinstance(body, dict):
        return body
    result = await _queries(request).learner_query(
        body,
        request.headers.get("Idempotency-Key", ""),
        get_operation_context(request),
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/feishu/learner-query-result.schema.json",
        resource_identity={"learner_ref": body["learner_ref"]},
    )


@router.post(
    "/integrations/feishu/v1/class-insights",
    operation_id="queryClassInsightsFromFeishu",
)
async def query_feishu_class_insights(request: Request) -> Any:
    body = await _body(
        request,
        "contracts/schemas/feishu/class-insights-query.schema.json",
        operation="FEISHU_CLASS_INSIGHTS",
    )
    if not isinstance(body, dict):
        return body
    result = await _queries(request).class_insights(
        body,
        request.headers.get("Idempotency-Key", ""),
        get_operation_context(request),
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/feishu/class-insights-result.schema.json",
        resource_identity={"class_ref": body["class_ref"]},
        # This frozen schema explicitly admits fractional ratios and does not
        # declare YAYA_CANONICAL_JSON_V1 (whose number domain is integer-only).
        use_canonical_json=False,
    )


@router.get(
    "/integrations/feishu/v1/evidence/{evidence_id}",
    operation_id="getRedactedEvidenceForFeishu",
)
async def get_feishu_evidence(evidence_id: str, request: Request) -> Any:
    purpose = request.query_params.get("purpose", "")
    result = await _queries(request).redacted_evidence(
        evidence_id, purpose, get_operation_context(request)
    )
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    return contract_response(
        request=request,
        payload=result.value,
        schema_path="contracts/schemas/feishu/evidence-view.schema.json",
        resource_identity={"evidence_ref.evidence_id": evidence_id},
    )


__all__ = ["router"]
