"""Error-catalog responses for the public transport boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse

from walnut_backend.api.dependencies import OperationContext


class TransportError(Exception):
    def __init__(self, code: str, stage: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.stage = stage
        self.message = message


def attempt_headers(context: OperationContext) -> dict[str, str]:
    return {
        "X-Request-Id": context.request_id,
        "X-Trace-Id": context.trace_id,
        "X-Correlation-Id": context.correlation_id,
    }


def error_response(
    error: TransportError,
    context: OperationContext,
    catalog: Mapping[str, tuple[int, str, bool, str]],
) -> JSONResponse:
    """Create a schema-shaped error response only from locked catalog metadata."""
    code = error.code if error.code in catalog else "INTERNAL_ERROR"
    if code == "UNKNOWN_COMMIT_STATE" and error.stage != "WORLD_COMMIT":
        code = "INTERNAL_ERROR"
    http_status, category, retryable, message_key = catalog[code]
    body = error_body(code, error.stage, context, category, retryable, message_key, http_status)
    if not validate_error_response(catalog, body):
        code = "INTERNAL_ERROR"
        http_status, category, retryable, message_key = catalog[code]
        body = error_body(code, "TRANSPORT", context, category, retryable, message_key, http_status)
    headers = attempt_headers(context)
    if code == "UNKNOWN_COMMIT_STATE":
        headers["Location"] = f"/v1/commands/{context.command_id}"
    return JSONResponse(status_code=http_status, content=body, headers=headers)


def error_body(
    code: str,
    stage: str,
    context: OperationContext,
    category: str,
    retryable: bool,
    message_key: str,
    http_status: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": context.request_id,
        "trace_id": context.trace_id,
        "status": "UNKNOWN" if code == "UNKNOWN_COMMIT_STATE" else "FAILED" if http_status >= 500 else "REJECTED",
        "data": None,
        "error": {
            "code": code,
            "category": category,
            "retryable": retryable,
            "user_message_key": message_key,
            "stage": stage,
        },
    }
    if code == "UNKNOWN_COMMIT_STATE":
        body["command_id"] = context.command_id
    return body


def validate_error_response(
    catalog: Mapping[str, tuple[int, str, bool, str]], body: Mapping[str, Any]
) -> bool:
    """Use the release validator when the passed catalog is the locked catalog implementation."""
    validator = getattr(catalog, "validate_error_response", None)
    return callable(validator) and not validator(body)
