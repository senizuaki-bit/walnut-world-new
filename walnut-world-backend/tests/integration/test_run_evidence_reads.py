"""Public Run/Evidence reads reject unbacked or corrupt projection bytes."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext

from walnut_backend.adapters.postgres.models import request_context_data
from walnut_backend.adapters.postgres.run_evidence import PostgresRunEvidenceStore
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

AGENT_ROOT = DEFAULT_CONTRACT_PATH
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_actor",
    "X-Request-Id": "req_run_evidence_0001",
    "X-Trace-Id": "trace_run_evidence_0001",
    "X-Correlation-Id": "corr_run_evidence_0001",
    "X-Schema-Version": "1.0.0",
}


def test_run_and_evidence_reads_are_immutable_and_actor_scoped() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL Run/Evidence coverage")
    asyncio.run(_exercise_reads(database_url))


async def _exercise_reads(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    context = operation_context()
    run, evidence = example_resources(context)
    evidence_reference = evidence["evidence_ref"]
    evidence_integrity = evidence["integrity"]
    assert isinstance(evidence_reference, dict)
    assert isinstance(evidence_integrity, dict)
    evidence_id = evidence_reference["evidence_id"]
    evidence_digest = evidence_integrity["payload_sha256"]
    assert isinstance(evidence_id, str)
    assert isinstance(evidence_digest, str)
    try:
        store = PostgresRunEvidenceStore(sessions)
        assert (await store.record_run(run, context)).ok
        assert (await store.record_evidence(evidence, context)).ok
        settings = replace(Settings.for_test(contract_path=AGENT_ROOT), database_url=database_url)
        with TestClient(create_app(settings)) as client:
            run_response = client.get(f"/v1/runs/{run['run_id']}", headers=HEADERS)
            evidence_response = client.get(
                f"/v1/evidence/{evidence_id}", headers=HEADERS
            )
            denied = client.get(
                f"/v1/runs/{run['run_id']}",
                headers={**HEADERS, "Authorization": "Bearer tenant_yaya:student_other"},
            )
        assert run_response.status_code == 500
        assert run_response.json()["error"]["code"] == "INVARIANT_VIOLATION"
        assert evidence_response.status_code == 500
        assert evidence_response.json()["error"]["code"] == "INVARIANT_VIOLATION"
        assert denied.status_code == 404

        tampered = copy.deepcopy(evidence)
        tampered_reference = tampered["evidence_ref"]
        assert isinstance(tampered_reference, dict)
        tampered_reference["evidence_id"] = f"evidence_{uuid4().hex}"
        tampered_reference["sha256"] = "f" * 64
        if tampered_reference["sha256"] == evidence_digest:
            tampered_reference["sha256"] = "e" * 64
        assert (await store.record_evidence(tampered, context)).ok
        with TestClient(create_app(settings)) as client:
            tampered_response = client.get(
                f'/v1/evidence/{tampered_reference["evidence_id"]}', headers=HEADERS
            )
        assert tampered_response.status_code == 500
        assert tampered_response.json()["error"]["code"] == "INVARIANT_VIOLATION"
    finally:
        await sessions.kw["bind"].dispose()


def operation_context() -> OperationContext:
    return OperationContext(
        request_id="req_run_evidence_seed_0001",
        correlation_id="corr_run_evidence_seed_0001",
        trace_id="trace_run_evidence_seed_0001",
        requested_at=datetime.now(UTC),
        actor=ActorRef("tenant_yaya", "student_actor", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef("UNIT_TRANSPORT", "1.0.0", "0" * 64),
        command_id="cmd_run_evidence_seed_0001",
        causation_id=None,
    )


def example_resources(context: OperationContext) -> tuple[dict[str, object], dict[str, object]]:
    run = _example("game-run.json")
    evidence = _example("game-evidence.json")
    suffix = uuid4().hex
    run_id, evidence_id = f"run_{suffix}", f"evidence_{suffix}"
    command_id, session_id, turn_id = f"cmd_{suffix}", f"session_{suffix}", f"turn_{suffix}"
    origin = request_context_data(context)
    run["request_context"] = origin
    run["run_id"] = run_id
    run["command_id"] = command_id
    run["session_id"] = session_id
    run["turn_id"] = turn_id
    feedback = run["agent_feedback"]
    assert isinstance(feedback, dict)
    feedback.update({"run_id": run_id, "command_id": command_id, "session_id": session_id, "turn_id": turn_id})
    references = run["evidence_refs"]
    assert isinstance(references, list) and isinstance(references[0], dict)
    references[0]["evidence_id"] = evidence_id
    feedback_refs = feedback["evidence_refs"]
    assert isinstance(feedback_refs, list) and isinstance(feedback_refs[0], dict)
    feedback_refs[0]["evidence_id"] = evidence_id

    evidence["request_context"] = origin
    reference = evidence["evidence_ref"]
    assert isinstance(reference, dict)
    reference["evidence_id"] = evidence_id
    source = evidence["source"]
    assert isinstance(source, dict)
    source["command_id"] = command_id
    return run, evidence


def _example(name: str) -> dict[str, object]:
    wrapper = json.loads((AGENT_ROOT / "contracts/examples" / name).read_text(encoding="utf-8"))
    value = wrapper["value"]
    assert isinstance(value, dict)
    return value
