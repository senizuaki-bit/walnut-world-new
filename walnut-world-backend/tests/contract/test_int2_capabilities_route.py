"""INT2 capability GET remains available while every mutation gate defaults closed."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from yaya_agent_contracts import (
    ContentRef,
    ContractError,
    ErrorCategory,
    Failure,
    OperationContext,
    Success,
)

from walnut_backend.adapters.postgres.models import request_context_data
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_0001",
    "X-Request-Id": "req_int2caps_0001",
    "X-Trace-Id": "trace_int2caps_0001",
    "X-Correlation-Id": "corr_int2caps_0001",
    "X-Schema-Version": "1.0.0",
}
CONTENT_REF = ContentRef("UNIT_INT2_CAPABILITY", "1.0.0", "a" * 64)


class _ClosedStudentBootstrapQueries:
    async def get(self, context: OperationContext) -> Success[dict[str, Any]]:
        durable_context = replace(context, content_ref=CONTENT_REF)
        return Success({"request_context": request_context_data(durable_context)})


class _FailedStudentBootstrapQueries:
    async def get(self, context: OperationContext) -> Failure:
        del context
        return Failure(
            ContractError(
                code="INVARIANT_VIOLATION",
                category=ErrorCategory.INVARIANT,
                retryable=False,
                user_message_key="system.invariant_violation",
                stage="AUTHORITY",
                message="durable student launch authority is incomplete",
            )
        )


def _install_closed_bootstrap(app: FastAPI) -> None:
    app.state.student_bootstrap_queries = _ClosedStudentBootstrapQueries()


def test_int2_capabilities_default_closed_and_get_only() -> None:
    app = create_app(Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH))
    with TestClient(app) as client:
        _install_closed_bootstrap(app)
        response = client.get("/product-experience/v1/capabilities", headers=HEADERS)
        mutation = client.post("/product-experience/v1/capabilities", headers=HEADERS, json={})

    assert response.status_code == 200, response.text
    value = response.json()
    assert value["world_presentation_enabled"] is False
    assert value["skill_patch_enabled"] is False
    assert value["request_context"]["request_id"] == HEADERS["X-Request-Id"]
    assert value["request_context"]["trace_id"] == HEADERS["X-Trace-Id"]
    assert value["request_context"]["correlation_id"] == HEADERS["X-Correlation-Id"]
    assert value["request_context"]["content_ref"] == {
        "unit_id": CONTENT_REF.unit_id,
        "version": CONTENT_REF.version,
        "content_hash": CONTENT_REF.content_hash,
    }
    assert value["skill_patch_constraints"] == {
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
    }
    assert mutation.status_code == 405


def test_int2_capabilities_report_explicit_rollout_flags() -> None:
    settings = replace(
        Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH),
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        _install_closed_bootstrap(app)
        response = client.get("/product-experience/v1/capabilities", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["world_presentation_enabled"] is True
    assert response.json()["skill_patch_enabled"] is True


def test_int2_capabilities_fail_closed_with_bootstrap_authority_error() -> None:
    app = create_app(Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH))
    with TestClient(app) as client:
        app.state.student_bootstrap_queries = _FailedStudentBootstrapQueries()
        response = client.get("/product-experience/v1/capabilities", headers=HEADERS)

    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert error["code"] == "INVARIANT_VIOLATION"
    assert error["stage"] == "AUTHORITY"
    assert response.headers["X-Request-Id"] == HEADERS["X-Request-Id"]
    assert response.headers["X-Trace-Id"] == HEADERS["X-Trace-Id"]
    assert response.headers["X-Correlation-Id"] == HEADERS["X-Correlation-Id"]
