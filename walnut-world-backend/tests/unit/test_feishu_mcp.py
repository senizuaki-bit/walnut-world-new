"""Protocol, authority, and read-only tests for the stateless teacher MCP adapter."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from yaya_agent_contracts import ContentRef, ContractError, ErrorCategory, Failure, Success

from walnut_backend.api.app import create_app
from walnut_backend.application.feishu.learning_queries import (
    FeishuLearningQueries,
    stable_class_ref,
)
from walnut_backend.application.feishu.learning_sync import stable_business_key
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, ContractRelease, Settings

TENANT = "tenant_mcp"
LEARNER_REF = "lrn_12345678"
EVIDENCE_ID = "evidence_12345678"
CONTENT_REF = {
    "unit_id": "UNIT_MCP",
    "version": "1.0.0",
    "content_hash": "c" * 64,
}


class RecordingQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any, Any]] = []
        self.deny = False

    async def resolve_learner_content_ref(self, learner_ref, context):
        self.calls.append(("resolve_learner", learner_ref, None, context))
        return Success(ContentRef(**CONTENT_REF))

    async def resolve_tenant_content_ref(self, context, class_ref):
        self.calls.append(("resolve_class", class_ref, None, context))
        return Success(ContentRef(**CONTENT_REF))

    async def learner_query(self, body, idempotency_key, context):
        self.calls.append(("learner", body, idempotency_key, context))
        if self.deny:
            return Failure(_denied())
        return Success(
            {
                "learner_ref": body["learner_ref"],
                "activity_summary": {"sessions": 1, "completed_tasks": 1},
                "trace_id": context.trace_id,
            }
        )

    async def class_insights(self, body, idempotency_key, context):
        self.calls.append(("class", body, idempotency_key, context))
        return Success(
            {
                "class_ref": body["class_ref"],
                "cohort_size": 8,
                "insights": [],
                "trace_id": context.trace_id,
            }
        )

    async def redacted_evidence(self, evidence_id, purpose, context):
        self.calls.append(("evidence", evidence_id, purpose, context))
        return Success(
            {
                "evidence_ref": {"evidence_id": evidence_id},
                "learner_ref": LEARNER_REF,
                "summary": "任务已完成",
                "facts": [{"name": "attempt_count", "value": 1}],
                "trace_id": context.trace_id,
            }
        )


class NoReadStore:
    def __init__(self) -> None:
        self.reads = 0
        self.audits: list[dict[str, Any]] = []

    async def learner_content_refs(self, *args, **kwargs):
        self.reads += 1
        raise AssertionError("denied MCP call reached learner content authority")

    async def tenant_content_refs(self, *args, **kwargs):
        self.reads += 1
        raise AssertionError("denied MCP call reached class content authority")

    async def learner_bundle(self, *args, **kwargs):
        self.reads += 1
        raise AssertionError("denied MCP call reached learner authority")

    async def class_bundles(self, *args, **kwargs):
        self.reads += 1
        raise AssertionError("denied MCP call reached class authority")

    async def evidence_bundle(self, *args, **kwargs):
        self.reads += 1
        raise AssertionError("denied MCP call reached evidence authority")

    async def append_access_audit(self, **values):
        self.audits.append(values)
        return Success(None)

def test_initialize_and_list_exactly_three_read_only_tools() -> None:
    app, queries = _app()
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        initialized = client.post(
            "/integrations/feishu/v1/mcp",
            headers=_headers(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "aily", "version": "1.0.0"},
                },
            },
        )
        listed = client.post(
            "/integrations/feishu/v1/mcp",
            headers=_headers(),
            json={"jsonrpc": "2.0", "id": "list-1", "method": "tools/list", "params": {}},
        )
        notification = client.post(
            "/integrations/feishu/v1/mcp",
            headers=_headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        list_notification = client.post(
            "/integrations/feishu/v1/mcp",
            headers=_headers(),
            json={"jsonrpc": "2.0", "method": "tools/list", "params": {}},
        )
        ping = client.post(
            "/integrations/feishu/v1/mcp",
            headers=_headers(),
            json={"jsonrpc": "2.0", "id": "ping-1", "method": "ping"},
        )
        null_id = client.post(
            "/integrations/feishu/v1/mcp",
            headers=_headers(),
            json={"jsonrpc": "2.0", "id": None, "method": "tools/list"},
        )

    assert initialized.status_code == 200
    assert initialized.headers["MCP-Protocol-Version"] == "2025-06-18"
    assert initialized.json()["result"]["capabilities"] == {"tools": {"listChanged": False}}
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "query_learner_progress",
        "query_class_common_issues",
        "get_evidence_summary_and_links",
    ]
    assert all(
        tool["annotations"]
        == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        for tool in tools
    )
    assert all("tenant" not in tool["inputSchema"]["properties"] for tool in tools)
    assert tools[0]["inputSchema"]["required"] == ["learner_ref"]
    assert "required" not in tools[1]["inputSchema"]
    assert "recent_evidence.evidence_id" in tools[2]["description"]
    assert notification.status_code == 202
    assert notification.content == b""
    assert list_notification.status_code == 202
    assert list_notification.content == b""
    assert ping.json() == {"jsonrpc": "2.0", "id": "ping-1", "result": {}}
    assert null_id.status_code == 400
    assert null_id.json()["error"] == {"code": -32600, "message": "Invalid Request"}
    assert queries.calls == []


def test_initialize_negotiates_aily_http_streaming_protocol() -> None:
    app, queries = _app()
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        response = client.post(
            "/integrations/feishu/v1/mcp",
            headers=_headers(),
            json={
                "jsonrpc": "2.0",
                "id": "aily-init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "aily", "version": "1.0.0"},
                },
            },
        )

    assert response.status_code == 200
    assert response.headers["MCP-Protocol-Version"] == "2025-03-26"
    assert response.json()["result"]["protocolVersion"] == "2025-03-26"
    assert queries.calls == []


def test_standard_mcp_headers_receive_server_generated_attempt_context() -> None:
    app, queries = _app()
    standard_headers = {
        "Authorization": f"Bearer {TENANT}:feishu_teacher_mcp",
        "MCP-Protocol-Version": "2025-06-18",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        response = client.post(
            "/integrations/feishu/v1/mcp",
            headers=standard_headers,
            json={"jsonrpc": "2.0", "id": "list-standard", "method": "tools/list"},
        )

    assert response.status_code == 200
    assert len(response.json()["result"]["tools"]) == 3
    assert response.headers["X-Request-Id"].startswith("req_")
    assert response.headers["X-Trace-Id"].startswith("trace_")
    assert response.headers["X-Correlation-Id"].startswith("corr_")


def test_learner_and_class_calls_derive_trusted_context_and_fixed_policy() -> None:
    app, queries = _app()
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        learner = _call(
            client,
            "query_learner_progress",
            {"learner_ref": LEARNER_REF, "content_ref": CONTENT_REF},
            request_id="learner-1",
        )
        class_result = _call(
            client,
            "query_class_common_issues",
            {
                "class_ref": "cls_12345678",
                "content_ref": CONTENT_REF,
                "time_range": {
                    "from": "2026-08-09T00:00:00Z",
                    "to": "2026-08-16T00:00:00Z",
                },
            },
            request_id="class-1",
        )

    assert learner.status_code == class_result.status_code == 200
    learner_result = learner.json()["result"]
    assert learner_result["isError"] is False
    assert learner_result["structuredContent"]["fact_type"] == "LEARNER_PROGRESS"
    assert learner_result["content"][0]["text"] == _compact(
        learner_result["structuredContent"]
    )
    learner_call = queries.calls[0]
    body = learner_call[1]
    context = learner_call[3]
    assert body["context"]["actor"] == {
        "tenant_id": TENANT,
        "actor_id": "feishu_teacher_mcp",
        "actor_type": "teacher",
        "roles": list(context.actor.roles),
    }
    assert body["context"]["content_ref"] == CONTENT_REF
    assert body["purpose"] == "TEACHER_SUPPORT"
    assert body["consent_basis"] == "EDUCATIONAL_SERVICE"
    assert learner_call[2].startswith("mcp:query_learner_progress:req_mcp_")
    class_body = queries.calls[1][1]
    assert class_body["purpose"] == "TEACHER_PLANNING"
    assert class_body["privacy"] == {
        "minimum_cohort_size": 5,
        "suppress_small_cells": True,
    }
    release = ContractRelease(app.state.settings)
    assert release.validate("contracts/schemas/feishu/learner-query.schema.json", body) == []
    assert (
        release.validate("contracts/schemas/feishu/class-insights-query.schema.json", class_body)
        == []
    )
    assert class_result.json()["result"]["structuredContent"]["links"] == {
        "dashboard_url": "https://example.feishu.cn/base/Base?table=Dashboard"
    }


def test_minimal_inputs_resolve_authority_and_default_shanghai_seven_day_window() -> None:
    app, queries = _app()
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        learner = _call(
            client,
            "query_learner_progress",
            {"learner_ref": LEARNER_REF},
            request_id="learner-minimal",
        )
        class_result = _call(
            client,
            "query_class_common_issues",
            {},
            request_id="class-minimal",
        )

    assert learner.json()["result"]["isError"] is False
    assert class_result.json()["result"]["isError"] is False
    assert [call[0] for call in queries.calls] == [
        "resolve_learner",
        "learner",
        "resolve_class",
        "class",
    ]
    learner_body = queries.calls[1][1]
    class_body = queries.calls[3][1]
    class_context = queries.calls[3][3]
    assert learner_body["context"]["content_ref"] == CONTENT_REF
    assert class_body["context"]["content_ref"] == CONTENT_REF
    assert class_body["class_ref"] == stable_class_ref(
        app.state.settings.resolved_feishu_pseudonym_secret(), TENANT
    )
    requested_at = class_context.requested_at
    shanghai = ZoneInfo("Asia/Shanghai")
    local_now = requested_at.astimezone(shanghai)
    expected_start = datetime.combine(
        local_now.date() - timedelta(days=6), time.min, tzinfo=shanghai
    ).astimezone(UTC)
    assert class_body["time_range"] == {
        "from": expected_start.isoformat().replace("+00:00", "Z"),
        "to": requested_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    release = ContractRelease(app.state.settings)
    assert release.validate(
        "contracts/schemas/feishu/learner-query.schema.json", learner_body
    ) == []
    assert release.validate(
        "contracts/schemas/feishu/class-insights-query.schema.json", class_body
    ) == []


def test_evidence_call_returns_only_server_derived_trusted_links() -> None:
    app, queries = _app()
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        response = _call(
            client,
            "get_evidence_summary_and_links",
            {"evidence_id": EVIDENCE_ID},
            request_id="evidence-1",
        )

    structured = response.json()["result"]["structuredContent"]
    learner_key = stable_business_key(
        app.state.settings.resolved_feishu_pseudonym_secret(),
        "fsp",
        TENANT,
        LEARNER_REF,
    )
    evidence_key = stable_business_key(
        app.state.settings.resolved_feishu_pseudonym_secret(),
        "fev",
        TENANT,
        EVIDENCE_ID,
    )
    profile_url = f"https://teacher.example/students/{learner_key}"
    assert structured == {
        "fact_type": "REDACTED_EVIDENCE",
        "evidence": {
            "evidence_ref": {"evidence_id": EVIDENCE_ID},
            "learner_ref": LEARNER_REF,
            "summary": "任务已完成",
            "facts": [{"name": "attempt_count", "value": 1}],
            "trace_id": "trace_mcp_12345678",
        },
        "links": {
            "student_detail_url": profile_url,
            "dashboard_url": "https://example.feishu.cn/base/Base?table=Dashboard",
            "evidence_url": f"{profile_url}#evidence-{evidence_key}",
        },
    }
    assert queries.calls[0][0:3] == ("evidence", EVIDENCE_ID, "TEACHER_SUPPORT")


def test_unknown_tool_extra_arguments_and_bad_protocol_fail_before_queries() -> None:
    app, queries = _app()
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        unknown = _call(client, "delete_student", {}, request_id="unknown-1")
        extra = _call(
            client,
            "get_evidence_summary_and_links",
            {"evidence_id": EVIDENCE_ID, "tenant_id": "tenant_other"},
            request_id="extra-1",
        )
        bad_protocol = client.post(
            "/integrations/feishu/v1/mcp",
            headers={**_headers(), "MCP-Protocol-Version": "2099-01-01"},
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        )

    assert unknown.json()["error"] == {"code": -32602, "message": "Unknown teacher tool"}
    assert extra.json()["error"] == {
        "code": -32602,
        "message": "Invalid teacher tool arguments",
    }
    assert bad_protocol.status_code == 400
    assert bad_protocol.json()["error"]["code"] == -32600
    assert queries.calls == []


def test_application_denial_is_safe_mcp_tool_error_without_internal_message() -> None:
    app, queries = _app()
    queries.deny = True
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        response = _call(
            client,
            "query_learner_progress",
            {"learner_ref": LEARNER_REF, "content_ref": CONTENT_REF},
            request_id="denied-1",
        )

    result = response.json()["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result
    error = __import__("json").loads(result["content"][0]["text"])["error"]
    assert error == {
        "code": "AUTHORIZATION_DENIED",
        "category": "AUTHORIZATION",
        "retryable": False,
        "user_message_key": "auth.permission_denied",
        "stage": "AUTHORITY",
    }
    assert "sensitive internal reason" not in result["content"][0]["text"]


def test_student_role_and_cross_tenant_class_are_denied_audited_before_reads() -> None:
    app, _ = _app()
    store = NoReadStore()
    with TestClient(app) as client:
        secret = app.state.settings.resolved_feishu_pseudonym_secret()
        queries = FeishuLearningQueries(store, pseudonym_secret=secret)
        app.state.feishu_learning_queries = queries
        student = _call(
            client,
            "query_learner_progress",
            {"learner_ref": LEARNER_REF},
            request_id="student-denied",
            actor_id="student_mcp",
        )
        cross_tenant = _call(
            client,
            "query_class_common_issues",
            {
                "class_ref": stable_class_ref(secret, "tenant_other"),
            },
            request_id="tenant-denied",
        )

    assert student.json()["result"]["isError"] is True
    assert cross_tenant.json()["result"]["isError"] is True
    assert store.reads == 0
    assert [item["outcome"] for item in store.audits] == ["DENIED", "DENIED"]
    assert [item["error_code"] for item in store.audits] == [
        "AUTHORIZATION_DENIED",
        "AUTHORIZATION_DENIED",
    ]


def test_transport_auth_origin_body_limit_and_get_are_fail_closed() -> None:
    app, queries = _app()
    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        unauthenticated = client.post(
            "/integrations/feishu/v1/mcp",
            headers={key: value for key, value in _headers().items() if key != "Authorization"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        origin = client.post(
            "/integrations/feishu/v1/mcp",
            headers={**_headers(), "Origin": "https://untrusted.example"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        malformed = client.post(
            "/integrations/feishu/v1/mcp",
            headers={**_headers(), "Content-Type": "application/json"},
            content=b"{",
        )
        oversized = client.post(
            "/integrations/feishu/v1/mcp",
            headers={**_headers(), "Content-Type": "application/json"},
            content=b" " * (64 * 1024 + 1),
        )
        get_response = client.get("/integrations/feishu/v1/mcp", headers=_headers())

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert origin.status_code == 403
    assert origin.json()["error"]["code"] == "AUTHORIZATION_DENIED"
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == -32700
    assert oversized.status_code == 413
    assert get_response.status_code == 405
    assert queries.calls == []


def test_mcp_link_settings_reject_non_https_or_credentialed_urls() -> None:
    base = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    for values in (
        {"feishu_mcp_dashboard_url": "http://example.test/dashboard"},
        {"feishu_mcp_teacher_workspace_url": "https://user:secret@example.test/app"},
        {"feishu_mcp_teacher_workspace_url": "https://example.test/app?token=secret"},
    ):
        try:
            replace(base, **values)
        except ValueError as error:
            assert "credential-free HTTPS URL" in str(error)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("unsafe MCP link setting was accepted")


def _app() -> tuple[Any, RecordingQueries]:
    settings = replace(
        Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH),
        feishu_mcp_dashboard_url="https://example.feishu.cn/base/Base?table=Dashboard",
        feishu_mcp_teacher_workspace_url="https://teacher.example",
    )
    return create_app(settings), RecordingQueries()


def _headers(*, actor_id: str = "feishu_teacher_mcp") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TENANT}:{actor_id}",
        "X-Request-Id": "req_mcp_12345678",
        "X-Trace-Id": "trace_mcp_12345678",
        "X-Correlation-Id": "corr_mcp_12345678",
        "X-Schema-Version": "1.0.0",
        "MCP-Protocol-Version": "2025-06-18",
        "Accept": "application/json, text/event-stream",
    }


def _call(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
    *,
    request_id: str,
    actor_id: str = "feishu_teacher_mcp",
):
    return client.post(
        "/integrations/feishu/v1/mcp",
        headers=_headers(actor_id=actor_id),
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _denied() -> ContractError:
    return ContractError(
        "AUTHORIZATION_DENIED",
        ErrorCategory.AUTHORIZATION,
        False,
        "auth.permission_denied",
        "AUTHORITY",
        "sensitive internal reason",
    )


def _compact(value: Any) -> str:
    return __import__("json").dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
