"""Stateless MCP Streamable HTTP adapter for the three teacher read tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from typing import Any, Final, Literal, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft202012Validator
from yaya_agent_contracts import ContentRef, Failure

from walnut_backend.api.dependencies import OperationContext, get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.application.feishu.learning_queries import (
    FeishuLearningQueries,
    stable_class_ref,
)
from walnut_backend.application.feishu.learning_sync import stable_business_key

router = APIRouter()

MCP_PATH: Final = "/integrations/feishu/v1/mcp"
MCP_PROTOCOL_VERSION: Final = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS: Final = frozenset({"2025-03-26", MCP_PROTOCOL_VERSION})
MAX_REQUEST_BYTES: Final = 64 * 1024
ASIA_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")

LEARNER_TOOL: Final = "query_learner_progress"
CLASS_TOOL: Final = "query_class_common_issues"
EVIDENCE_TOOL: Final = "get_evidence_summary_and_links"

_CONTENT_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["unit_id", "version", "content_hash"],
    "properties": {
        "unit_id": {"type": "string", "pattern": "^[A-Z0-9][A-Z0-9_-]{2,79}$"},
        "version": {
            "type": "string",
            "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(?:-[0-9A-Za-z.-]+)?$",
        },
        "content_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    },
}
_TIME_RANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["from", "to"],
    "properties": {
        "from": {"type": "string", "format": "date-time", "maxLength": 64},
        "to": {"type": "string", "format": "date-time", "maxLength": 64},
    },
}

TOOLS: Final = (
    {
        "name": LEARNER_TOOL,
        "title": "查询学生学习进度",
        "description": (
            "查询已脱敏的学生掌握、活动、Evidence、支持需求和建议事实。"
            "只需提供 learner_ref；content_ref 仅在多内容时用于消歧。"
            "只读；不接受姓名、租户、角色或任意用途。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["learner_ref"],
            "properties": {
                "learner_ref": {
                    "type": "string",
                    "pattern": "^lrn_[A-Za-z0-9_-]{8,128}$",
                },
                "content_ref": _CONTENT_REF_SCHEMA,
                "time_range": _TIME_RANGE_SCHEMA,
            },
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": CLASS_TOOL,
        "title": "查询班级共性问题",
        "description": (
            "查询班级知识点、高频错误、支持需求、活跃和完成分布。"
            "class_ref、content_ref、time_range 均可省略，由 Backend 从已认证租户、"
            "唯一 Learner Profile 内容和上海时区最近 7 个自然日解析。"
            "小样本单元由 Backend 强制抑制，不返回学生标识。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "class_ref": {
                    "type": "string",
                    "pattern": "^cls_[A-Za-z0-9_-]{8,128}$",
                },
                "content_ref": _CONTENT_REF_SCHEMA,
                "time_range": _TIME_RANGE_SCHEMA,
            },
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": EVIDENCE_TOOL,
        "title": "查看证据摘要及档案/Dashboard链接",
        "description": (
            "使用查询学生学习进度返回的 recent_evidence.evidence_id，查看该 Evidence "
            "的白名单事实、来源和脱敏声明；不得猜测或自行构造 evidence_id。"
            "并返回受信任的教师工作台学生详情入口（可在页内打开成长档案）"
            "与 Dashboard 链接；不伪装成直达云文档链接。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["evidence_id"],
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "pattern": "^evidence_[A-Za-z0-9_-]{8,120}$",
                }
            },
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
)

_TOOLS_BY_NAME: Final = {str(tool["name"]): tool for tool in TOOLS}
_TOOL_VALIDATORS: Final = {
    name: Draft202012Validator(cast(dict[str, Any], tool["inputSchema"]))
    for name, tool in _TOOLS_BY_NAME.items()
}


def _queries(request: Request) -> FeishuLearningQueries:
    return request.app.state.feishu_learning_queries


@router.post(MCP_PATH, operation_id="feishuTeacherMcp")
async def feishu_teacher_mcp(request: Request) -> Response:
    """Handle one JSON-RPC message; the endpoint deliberately owns no session state."""
    context = get_operation_context(request)
    if request.headers.get("Origin") is not None:
        return error_response(
            TransportError("AUTHORIZATION_DENIED", "MCP_ORIGIN"),
            context,
            request.app.state.error_catalog,
        )
    content_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        return _rpc_error(None, -32600, "Content-Type must be application/json", status=415)
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                return _rpc_error(None, -32600, "Content-Length is invalid", status=400)
            if parsed_content_length > MAX_REQUEST_BYTES:
                return _rpc_error(None, -32600, "Request body is too large", status=413)
        except ValueError:
            return _rpc_error(None, -32600, "Content-Length is invalid", status=400)
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return _rpc_error(None, -32600, "Request body is too large", status=413)
    try:
        message = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _rpc_error(None, -32700, "Parse error", status=400)
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request", status=400)
    envelope = _rpc_envelope(message)
    if envelope is None:
        return _rpc_error(_safe_id(message.get("id")), -32600, "Invalid Request", status=400)
    request_id, method, params, is_notification = envelope
    if is_notification:
        return Response(status_code=202)
    protocol_header = request.headers.get("MCP-Protocol-Version")
    if method != "initialize" and protocol_header not in SUPPORTED_PROTOCOL_VERSIONS:
        return _rpc_error(request_id, -32600, "Unsupported MCP protocol version", status=400)

    if method == "initialize":
        if not _valid_initialize_params(params):
            return _rpc_error(request_id, -32602, "Invalid initialize parameters")
        requested = cast(str, cast(Mapping[str, Any], params)["protocolVersion"])
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        return _rpc_result(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "walnut-world-feishu-teacher", "version": "1.0.0"},
                "instructions": (
                    "仅使用三个教师只读工具。将回答分为客观事实、AI推断、教学建议；"
                    "客观事实不得超出工具结果，缺失时明确写暂无可核验数据。"
                ),
            },
            protocol_version=negotiated,
        )
    if method == "notifications/initialized":
        return _rpc_error(request_id, -32602, "initialized must be a notification")
    if method == "ping":
        if params not in ({}, None):
            return _rpc_error(request_id, -32602, "Invalid ping parameters")
        return _rpc_result(request_id, {})
    if method == "tools/list":
        if params not in ({}, None):
            return _rpc_error(request_id, -32602, "Invalid tools/list parameters")
        return _rpc_result(request_id, {"tools": list(TOOLS)})
    if method == "tools/call":
        parsed_call = _tool_call(params)
        if parsed_call is None:
            return _rpc_error(request_id, -32602, "Invalid tools/call parameters")
        name, arguments = parsed_call
        validator = _TOOL_VALIDATORS.get(name)
        if validator is None:
            return _rpc_error(request_id, -32602, "Unknown teacher tool")
        if next(validator.iter_errors(arguments), None) is not None:
            return _rpc_error(request_id, -32602, "Invalid teacher tool arguments")
        result = await _call_tool(request, context, name, arguments)
        return _rpc_result(request_id, result)
    return _rpc_error(request_id, -32601, "Method not found")


async def _call_tool(
    request: Request,
    context: OperationContext,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    if name == LEARNER_TOOL:
        supplied_content = arguments.get("content_ref")
        if supplied_content is None:
            resolved = await _queries(request).resolve_learner_content_ref(
                cast(str, arguments["learner_ref"]), context
            )
            if isinstance(resolved, Failure):
                return _tool_failure(request, resolved)
            content_ref = _content_ref_mapping(resolved.value)
        else:
            content_ref = cast(Mapping[str, Any], supplied_content)
        body: dict[str, Any] = {
            "context": _request_context(context, content_ref),
            "learner_ref": arguments["learner_ref"],
            "purpose": "TEACHER_SUPPORT",
            "requested_fields": [
                "MASTERY_SUMMARY",
                "RECENT_EVIDENCE",
                "SUPPORT_NEEDS",
                "ACTIVITY_SUMMARY",
                "RECOMMENDED_NEXT_STEPS",
                "DATA_FRESHNESS",
            ],
            "consent_basis": "EDUCATIONAL_SERVICE",
        }
        if "time_range" in arguments:
            body["time_range"] = arguments["time_range"]
        result = await _queries(request).learner_query(body, _idempotency_key(context, name), context)
        if isinstance(result, Failure):
            return _tool_failure(request, result)
        payload = {
            "fact_type": "LEARNER_PROGRESS",
            "learner": dict(result.value),
            "links": _learner_links(request, cast(str, arguments["learner_ref"])),
        }
        return _tool_success(payload)
    if name == CLASS_TOOL:
        class_ref = cast(
            str,
            arguments.get("class_ref")
            or stable_class_ref(
                request.app.state.settings.resolved_feishu_pseudonym_secret(),
                context.actor.tenant_id,
            ),
        )
        supplied_content = arguments.get("content_ref")
        if supplied_content is None:
            resolved = await _queries(request).resolve_tenant_content_ref(
                context, class_ref
            )
            if isinstance(resolved, Failure):
                return _tool_failure(request, resolved)
            content_ref = _content_ref_mapping(resolved.value)
        else:
            content_ref = cast(Mapping[str, Any], supplied_content)
        body = {
            "context": _request_context(context, content_ref),
            "class_ref": class_ref,
            "purpose": "TEACHER_PLANNING",
            "time_range": arguments.get("time_range")
            or _default_class_time_range(context.requested_at),
            "dimensions": [
                "CONCEPT_MASTERY",
                "COMMON_ERRORS",
                "SUPPORT_NEEDS",
                "ENGAGEMENT",
                "COMPLETION",
            ],
            "privacy": {"minimum_cohort_size": 5, "suppress_small_cells": True},
        }
        result = await _queries(request).class_insights(
            body, _idempotency_key(context, name), context
        )
        if isinstance(result, Failure):
            return _tool_failure(request, result)
        payload = {
            "fact_type": "CLASS_COMMON_ISSUES",
            "class_insights": dict(result.value),
            "links": {"dashboard_url": request.app.state.settings.feishu_mcp_dashboard_url},
        }
        return _tool_success(payload)

    evidence_id = cast(str, arguments["evidence_id"])
    result = await _queries(request).redacted_evidence(
        evidence_id, "TEACHER_SUPPORT", context
    )
    if isinstance(result, Failure):
        return _tool_failure(request, result)
    learner_ref = cast(str, result.value["learner_ref"])
    payload = {
        "fact_type": "REDACTED_EVIDENCE",
        "evidence": dict(result.value),
        "links": _evidence_links(request, learner_ref, evidence_id),
    }
    return _tool_success(payload)


def _request_context(
    context: OperationContext, content_ref: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": context.requested_at.isoformat().replace("+00:00", "Z"),
        "actor": {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        },
        "content_ref": dict(content_ref),
    }


def _content_ref_mapping(reference: ContentRef) -> dict[str, str]:
    return {
        "unit_id": reference.unit_id,
        "version": reference.version,
        "content_hash": reference.content_hash,
    }


def _default_class_time_range(requested_at: datetime) -> dict[str, str]:
    """Return Shanghai today plus the preceding six calendar days through request time."""
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("requested_at must include a timezone")
    now = requested_at.astimezone(ASIA_SHANGHAI)
    local_start = datetime.combine(
        now.date() - timedelta(days=6),
        time.min,
        tzinfo=ASIA_SHANGHAI,
    )
    return {
        "from": local_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "to": requested_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def _learner_links(request: Request, learner_ref: str) -> dict[str, str | None]:
    workspace = request.app.state.settings.feishu_mcp_teacher_workspace_url
    profile_url: str | None = None
    if workspace is not None:
        learner_key = stable_business_key(
            request.app.state.settings.resolved_feishu_pseudonym_secret(),
            "fsp",
            get_operation_context(request).actor.tenant_id,
            learner_ref,
        )
        profile_url = f"{workspace.rstrip('/')}/students/{quote(learner_key, safe='')}"
    return {
        "student_detail_url": profile_url,
        "dashboard_url": request.app.state.settings.feishu_mcp_dashboard_url,
    }


def _evidence_links(
    request: Request, learner_ref: str, evidence_id: str
) -> dict[str, str | None]:
    links = _learner_links(request, learner_ref)
    profile_url = links["student_detail_url"]
    evidence_url: str | None = None
    if profile_url is not None:
        evidence_key = stable_business_key(
            request.app.state.settings.resolved_feishu_pseudonym_secret(),
            "fev",
            get_operation_context(request).actor.tenant_id,
            evidence_id,
        )
        evidence_url = f"{profile_url}#evidence-{quote(evidence_key, safe='')}"
    return {**links, "evidence_url": evidence_url}


def _tool_success(payload: Mapping[str, Any]) -> dict[str, Any]:
    structured = dict(payload)
    return {
        "content": [{"type": "text", "text": _json_text(structured)}],
        "structuredContent": structured,
        "isError": False,
    }


def _tool_failure(request: Request, failure: Failure) -> dict[str, Any]:
    error = failure.error
    code = error.code if error.code in request.app.state.error_catalog else "INTERNAL_ERROR"
    _, category, retryable, message_key = request.app.state.error_catalog[code]
    payload = {
        "error": {
            "code": code,
            "category": category,
            "retryable": retryable,
            "user_message_key": message_key,
            "stage": error.stage,
        }
    }
    return {"content": [{"type": "text", "text": _json_text(payload)}], "isError": True}


def _idempotency_key(context: OperationContext, name: str) -> str:
    return f"mcp:{name}:{context.request_id}"


def _rpc_envelope(
    value: Mapping[str, Any],
) -> tuple[str | int | None, str, Mapping[str, Any] | None, bool] | None:
    if value.get("jsonrpc") != "2.0" or not isinstance(value.get("method"), str):
        return None
    if set(value) - {"jsonrpc", "id", "method", "params"}:
        return None
    request_id = _safe_id(value.get("id"))
    if "id" in value and request_id is None:
        return None
    params = value.get("params")
    if params is not None and not isinstance(params, Mapping):
        return None
    return request_id, cast(str, value["method"]), cast(Mapping[str, Any] | None, params), "id" not in value


def _safe_id(value: object) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if len(value) <= 128 else None
    if isinstance(value, int):
        return value if -(2**63) <= value <= 2**63 - 1 else None
    return None


def _valid_initialize_params(params: Mapping[str, Any] | None) -> bool:
    if params is None or set(params) - {"protocolVersion", "capabilities", "clientInfo", "_meta"}:
        return False
    protocol = params.get("protocolVersion")
    capabilities = params.get("capabilities")
    client = params.get("clientInfo")
    return (
        isinstance(protocol, str)
        and len(protocol) <= 32
        and isinstance(capabilities, Mapping)
        and isinstance(client, Mapping)
        and isinstance(client.get("name"), str)
        and 1 <= len(cast(str, client["name"])) <= 128
        and isinstance(client.get("version"), str)
        and 1 <= len(cast(str, client["version"])) <= 64
    )


def _tool_call(params: Mapping[str, Any] | None) -> tuple[str, Mapping[str, Any]] | None:
    if params is None or set(params) - {"name", "arguments", "_meta"}:
        return None
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, Mapping):
        return None
    return name, cast(Mapping[str, Any], arguments)


def _rpc_result(
    request_id: str | int | None,
    result: Mapping[str, Any],
    *,
    protocol_version: str | None = None,
) -> JSONResponse:
    headers = {"MCP-Protocol-Version": protocol_version} if protocol_version is not None else None
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "result": dict(result)},
        headers=headers,
    )


def _rpc_error(
    request_id: str | int | None,
    code: int,
    message: Literal[
        "Content-Type must be application/json",
        "Request body is too large",
        "Content-Length is invalid",
        "Parse error",
        "Invalid Request",
        "Unsupported MCP protocol version",
        "Invalid initialize parameters",
        "Invalid initialized notification",
        "initialized must be a notification",
        "Invalid ping parameters",
        "Invalid tools/list parameters",
        "tools/call requires an id",
        "Invalid tools/call parameters",
        "Unknown teacher tool",
        "Invalid teacher tool arguments",
        "Method not found",
    ],
    *,
    status: int = 200,
) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
        status_code=status,
    )


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


__all__ = ["router"]
