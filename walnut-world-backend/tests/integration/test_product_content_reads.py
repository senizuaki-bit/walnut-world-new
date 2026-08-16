"""Exact immutable Product ContentUnit retrieval."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext

from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

AGENT_ROOT = DEFAULT_CONTRACT_PATH
HEADERS = {"Authorization": "Bearer tenant_yaya:student_content", "X-Request-Id": "req_product_content_0001", "X-Trace-Id": "trace_product_content_0001", "X-Correlation-Id": "corr_product_content_0001", "X-Schema-Version": "1.0.0"}


def test_product_content_requires_exact_immutable_reference() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for Product Content PostgreSQL coverage")
    settings = replace(Settings.for_test(contract_path=AGENT_ROOT), database_url=database_url)
    with TestClient(create_app(settings)) as client:
        content = json.loads((AGENT_ROOT / "contracts/examples/product-content-unit.json").read_text(encoding="utf-8"))["value"]
        context = OperationContext(request_id="req_content_seed", correlation_id="corr_content_seed", trace_id="trace_content_seed", requested_at=datetime.now(UTC), actor=ActorRef("tenant_yaya", "student_content", ActorType.STUDENT, ("game:player",)), content_ref=ContentRef(**content["content_ref"]), schema_version="1.0.0", command_id="cmd_content_seed_0001", causation_id=None)
        assert client.portal is not None
        outcome = client.portal.call(client.app.state.product_content._store.record_published, content, context)
        assert outcome.__class__.__name__ == "Success", outcome
        reference = content["content_ref"]
        path = f"/product-experience/v1/content-units/{reference['unit_id']}/versions/{reference['version']}?content_hash={reference['content_hash']}"
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert response.json() == content
        assert response.headers["etag"] == f'"{reference["content_hash"]}"'
        missing = client.get(path.replace("content_hash=", "content_hash=b"), headers=HEADERS)
        assert missing.status_code == 404
