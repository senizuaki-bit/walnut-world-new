"""PostgreSQL coverage for Product Agent interaction transcript reads."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext, SkillRef
from yaya_agent_runtime import side_effect_execution_id, skill_invocation_request_sha256

from tests.integration._product_workspace_support import seed_complete_product_workspace
from tests.integration._session_authority_support import seed_session_launch_authority
from tests.integration.test_int2_patch_vertical import _materialize_failure_chain
from tests.integration.test_terminal_read_closure import (
    _activate_and_read_skill,
    _execute_build,
    _TerminalBuild,
)
from walnut_backend.adapters.postgres.models import (
    AgentSessionRow,
    AgentTurnRow,
    CommandRow,
    EventRow,
    IdempotencyReceiptRow,
    JobStepReceiptRow,
    ProductInteractionRow,
    ProductWorkspaceRow,
    RunRow,
    WorkflowJobRow,
    command_record_data,
    command_record_from_data,
)
from walnut_backend.adapters.postgres.product_workspaces import refresh_workspace_in_session
from walnut_backend.adapters.postgres.workflow_jobs import (
    workflow_job_id,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)
from walnut_backend.api.app import create_app
from walnut_backend.api.routes.product_interactions import patch_decision_router
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.domain.canonical_json import canonical_payload

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = DEFAULT_CONTRACT_PATH
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_0001",
    "X-Request-Id": "req_product_interaction_0001",
    "X-Trace-Id": "trace_product_interaction_0001",
    "X-Correlation-Id": "corr_product_interaction_0001",
    "X-Schema-Version": "1.0.0",
}


def test_product_interaction_list_get_and_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for Product Interaction PostgreSQL coverage"
        )
    settings = replace(
        Settings.for_test(
            contract_path=AGENT_ROOT, contract_release_path=BACKEND_ROOT / "contract-release.json"
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal, session_id, interaction = _formal_interaction(
            client, database_url, tmp_path, monkeypatch
        )
        headers = terminal.headers
        list_path = f"/product-experience/v1/sessions/{session_id}/agent-interactions"

        page = client.get(list_path, headers=headers)
        assert page.status_code == 200, page.text
        assert page.headers["x-interaction-high-watermark"] == "1"
        assert page.json()["requested_after_sequence"] == 0
        assert page.json()["next_after_sequence"] == 1
        assert page.json()["interactions"] == [interaction]

        item_path = f"{list_path}/{interaction['interaction_id']}"
        item = client.get(item_path, headers=headers)
        assert item.status_code == 200, item.text
        assert item.json() == interaction
        assert item.headers["etag"].startswith('"interaction:1:')
        assert item.headers["x-interaction-revision"] == "1"

        empty = client.get(f"{list_path}?after_sequence=1", headers=headers)
        assert empty.status_code == 200, empty.text
        assert empty.json()["interactions"] == []
        assert empty.json()["high_watermark_sequence"] == 1

        ahead = client.get(f"{list_path}?after_sequence=2", headers=headers)
        assert ahead.status_code == 400
        assert ahead.json()["error"]["code"] == "INVALID_REQUEST"

        other_actor = client.get(
            item_path,
            headers={
                **headers,
                "Authorization": f"Bearer {terminal.tenant_id}:student_other",
            },
        )
        assert other_actor.status_code == 404


def test_legacy_patch_projection_is_rejected_without_immutable_authority() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for Product Interaction PostgreSQL coverage"
        )
    settings = replace(Settings.for_test(contract_path=AGENT_ROOT), database_url=database_url)
    app = create_app(settings)
    # PatchDecision is deliberately absent from the production Gateway.  This
    # focused compatibility test opts the dormant router in explicitly.
    app.include_router(patch_decision_router)
    with TestClient(app) as client:
        session_id = _create_session(client, database_url)
        draft_id, draft = _create_draft(client, session_id)
        interaction = _interaction_payload(session_id, include_patch=True)
        patch = interaction["skill_patch"]
        assert isinstance(patch, dict)
        patch_id = f"patch_{uuid4().hex}"
        patch.update(
            {
                "patch_id": patch_id,
                "draft_id": draft_id,
                "skill_id": draft["skill_id"],
                "base_draft_revision": draft["revision"],
                "base_draft_sha256": draft["draft_sha256"],
                "operations": [{"operation": "SET_DISPLAY_NAME", "display_name": "Patched skill"}],
            }
        )
        links = interaction["links"]
        assert isinstance(links, dict)
        links["skill_draft"] = (
            f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}"
        )
        patch["result_draft_sha256"] = _draft_hash(draft, "Patched skill")
        patch_without_hash = {key: value for key, value in patch.items() if key != "patch_sha256"}
        patch["patch_sha256"] = hashlib.sha256(canonical_payload(patch_without_hash)).hexdigest()
        source = interaction["projection_source"]
        links = interaction["links"]
        assert isinstance(source, dict)
        assert isinstance(links, dict)
        source["skill_patch_sha256"] = patch["patch_sha256"]
        links["skill_draft"] = (
            f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}"
        )
        _rehash_interaction(interaction)
        _record(client, interaction)

        body = {
            "decision_id": f"decision_{uuid4().hex}",
            "session_id": session_id,
            "turn_id": interaction["turn_id"],
            "interaction_id": interaction["interaction_id"],
            "expected_interaction_revision": 1,
            "patch_id": patch_id,
            "patch_sha256": patch["patch_sha256"],
            "draft_id": draft_id,
            "skill_id": draft["skill_id"],
            "base_draft_revision": 1,
            "base_draft_sha256": draft["draft_sha256"],
            "result_draft_sha256": patch["result_draft_sha256"],
            "decision": "ACCEPT",
            "reason_code": None,
            "decided_at": draft["updated_at"],
        }
        decision_path = (
            f"/product-experience/v1/sessions/{session_id}/agent-interactions/"
            f"{interaction['interaction_id']}/patches/{patch_id}/decision"
        )
        headers = {**HEADERS, "Idempotency-Key": f"idem_patch_{uuid4().hex}"}
        accepted = client.post(decision_path, headers=headers, json=body)
        assert accepted.status_code == 500, accepted.text
        assert accepted.json()["error"]["code"] == "INVARIANT_VIOLATION"
        draft_response = client.get(
            f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}", headers=HEADERS
        )
        assert draft_response.status_code == 200, draft_response.text
        assert draft_response.json()["revision"] == 1
        assert draft_response.json()["draft_sha256"] == draft["draft_sha256"]
        assert draft_response.json()["display_name"] == "Original skill"


def test_product_interaction_source_corruption_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for Product Interaction PostgreSQL coverage"
        )
    settings = replace(
        Settings.for_test(
            contract_path=AGENT_ROOT,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal, session_id, interaction = _formal_interaction(
            client, database_url, tmp_path, monkeypatch
        )
        headers = terminal.headers
        interaction_id = str(interaction["interaction_id"])
        source = interaction["projection_source"]
        feedback_event = interaction["feedback_event"]
        assert isinstance(source, dict)
        assert isinstance(feedback_event, dict)
        app = cast(FastAPI, client.app)
        assert client.portal is not None
        sessions = app.state.product_interactions._store._sessions
        command_id = str(source["command_id"])
        receipt_authority = client.portal.call(
            _read_interaction_receipt_authority,
            sessions,
            command_id,
        )
        assert (
            receipt_authority["job_request_sha256"] != receipt_authority["invocation_input_sha256"]
        )
        assert (
            receipt_authority["invocation_input_sha256"]
            == receipt_authority["invocation_request_sha256"]
            == receipt_authority["terminal_input_sha256"]
        )
        canonical_job_request_sha256 = receipt_authority["job_request_sha256"]
        assert isinstance(canonical_job_request_sha256, str)
        client.portal.call(
            _replace_job_request_sha256,
            sessions,
            command_id,
            "e" * 64,
        )
        _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)
        client.portal.call(
            _replace_job_request_sha256,
            sessions,
            command_id,
            canonical_job_request_sha256,
        )

        baseline_item = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}",
            headers=headers,
        )
        assert baseline_item.status_code == 200, baseline_item.text
        baseline_page = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions",
            headers=headers,
        )
        assert baseline_page.status_code == 200, baseline_page.text

        feedback_changed = copy.deepcopy(interaction)
        changed_feedback = feedback_changed["feedback"]
        assert isinstance(changed_feedback, dict)
        changed_feedback["message"] = "schema-valid substituted feedback"
        _rehash_interaction(feedback_changed)

        source_hash_changed = copy.deepcopy(interaction)
        changed_source = source_hash_changed["projection_source"]
        assert isinstance(changed_source, dict)
        changed_source["source_sha256"] = "f" * 64

        source_role_changed = copy.deepcopy(interaction)
        changed_role_source = source_role_changed["projection_source"]
        assert isinstance(changed_role_source, dict)
        source_role_changed["role"] = "system"
        changed_role_source["role"] = "system"
        _rehash_interaction(source_role_changed)

        run_id_changed = copy.deepcopy(interaction)
        changed_run_feedback = run_id_changed["feedback"]
        assert isinstance(changed_run_feedback, dict)
        changed_run_feedback["run_id"] = f"run_corrupt_{uuid4().hex}"
        _rehash_interaction(run_id_changed)

        for corrupted in (
            feedback_changed,
            source_hash_changed,
            source_role_changed,
            run_id_changed,
        ):
            client.portal.call(
                _replace_interaction_json,
                sessions,
                interaction_id,
                corrupted,
            )
            _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)
            client.portal.call(
                _replace_interaction_json,
                sessions,
                interaction_id,
                interaction,
            )

        event_id = str(feedback_event["event_id"])
        canonical_event = client.portal.call(_read_event_json, sessions, event_id)
        event_command_changed = copy.deepcopy(canonical_event)
        event_command_changed["command_id"] = f"cmd_corrupt_{uuid4().hex}"
        event_content_changed = copy.deepcopy(canonical_event)
        event_content = event_content_changed["content_ref"]
        assert isinstance(event_content, dict)
        event_content["version"] = "9.9.9"
        for corrupted_event in (event_command_changed, event_content_changed):
            client.portal.call(_replace_event_json, sessions, event_id, corrupted_event)
            _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)
            client.portal.call(_replace_event_json, sessions, event_id, canonical_event)

        canonical_command = client.portal.call(_read_command_json, sessions, command_id)
        command_evidence_changed = copy.deepcopy(canonical_command)
        command_evidence = command_evidence_changed["evidence_refs"]
        assert isinstance(command_evidence, list)
        assert isinstance(command_evidence[0], dict)
        command_evidence[0]["sha256"] = "e" * 64
        client.portal.call(
            _replace_command_json,
            sessions,
            command_id,
            command_evidence_changed,
        )
        _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)
        client.portal.call(
            _replace_command_json,
            sessions,
            command_id,
            canonical_command,
        )

        canonical_invocation_json = receipt_authority["invocation_receipt_json"]
        canonical_invocation_input = receipt_authority["invocation_input_sha256"]
        canonical_invocation_output = receipt_authority["invocation_output_sha256"]
        assert isinstance(canonical_invocation_json, dict)
        assert isinstance(canonical_invocation_input, str)
        assert isinstance(canonical_invocation_output, str)

        invocation_json_changed = copy.deepcopy(canonical_invocation_json)
        changed_invocation_run = invocation_json_changed["run"]
        assert isinstance(changed_invocation_run, dict)
        changed_invocation_run["run_id"] = f"run_corrupt_{uuid4().hex}"
        changed_invocation_output = workflow_receipt_sha256(invocation_json_changed)
        client.portal.call(
            _replace_skill_invocation_receipt,
            sessions,
            command_id,
            canonical_invocation_input,
            changed_invocation_output,
            invocation_json_changed,
        )
        _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)

        client.portal.call(
            _replace_skill_invocation_receipt,
            sessions,
            command_id,
            canonical_invocation_input,
            "e" * 64,
            canonical_invocation_json,
        )
        _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)

        client.portal.call(
            _replace_skill_invocation_receipt,
            sessions,
            command_id,
            "c" * 64,
            canonical_invocation_output,
            canonical_invocation_json,
        )
        _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)
        client.portal.call(
            _replace_skill_invocation_receipt,
            sessions,
            command_id,
            canonical_invocation_input,
            canonical_invocation_output,
            canonical_invocation_json,
        )

        coordinated_hash = "c" * 64
        coordinated_invocation_json = copy.deepcopy(canonical_invocation_json)
        coordinated_invocation_json["request_sha256"] = coordinated_hash
        coordinated_output_sha256 = workflow_receipt_sha256(coordinated_invocation_json)
        client.portal.call(
            _replace_skill_invocation_receipt,
            sessions,
            command_id,
            coordinated_hash,
            coordinated_output_sha256,
            coordinated_invocation_json,
        )
        client.portal.call(
            _replace_terminal_receipt_input,
            sessions,
            command_id,
            coordinated_hash,
        )
        _assert_interaction_reads_rejected(client, session_id, interaction_id, headers)
        client.portal.call(
            _replace_terminal_receipt_input,
            sessions,
            command_id,
            canonical_invocation_input,
        )
        client.portal.call(
            _replace_skill_invocation_receipt,
            sessions,
            command_id,
            canonical_invocation_input,
            canonical_invocation_output,
            canonical_invocation_json,
        )


def test_patch_decision_route_is_not_published_by_default() -> None:
    settings = Settings.for_test(contract_path=AGENT_ROOT)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/product-experience/v1/sessions/session_closed/agent-interactions/"
            "interaction_closed/patches/patch_closed/decision",
            headers={**HEADERS, "Idempotency-Key": "idem_patch_route_closed_0001"},
            json={},
        )
    assert response.status_code == 404


def test_patch_decision_route_requires_explicit_feature_flag() -> None:
    settings = replace(
        Settings.for_test(contract_path=AGENT_ROOT),
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/product-experience/v1/sessions/session_closed/agent-interactions/"
            "interaction_closed/patches/patch_closed/decision",
            headers={**HEADERS, "Idempotency-Key": "idem_patch_route_enabled_0001"},
            json={},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def _formal_interaction(
    client: TestClient,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_TerminalBuild, str, dict[str, object]]:
    terminal = _execute_build(
        client,
        database_url=database_url,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        succeed=True,
    )
    _, operation = _activate_and_read_skill(client, terminal)
    operation = replace(
        operation,
        actor=ActorRef(
            terminal.tenant_id,
            terminal.actor_id,
            ActorType.STUDENT,
            ("game:player",),
        ),
    )
    chain = _materialize_failure_chain(client, terminal, operation, count=1)
    return terminal, chain.session_id, cast(dict[str, object], chain.interactions[0])


def _create_session(client: TestClient, database_url: str) -> str:
    request = {
        "world_id": "world_product_interaction",
        "learner_id": "learner_product_0001",
        "agent_profile_id": "agent_product_0001",
        "channel": "GAME",
        "locale": "zh-CN",
        "content": {
            "unit_id": "YAYA_FARM_001",
            "version": "1.4.0",
            "content_hash": "a" * 64,
        },
    }
    asyncio.run(
        seed_session_launch_authority(
            database_url,
            tenant_id="tenant_yaya",
            actor_id="student_0001",
            request=request,
        )
    )
    response = client.post(
        "/v1/agent-sessions",
        headers={**HEADERS, "Idempotency-Key": f"idem_product_interaction_{uuid4().hex}"},
        json=request,
    )
    assert response.status_code == 202, response.text
    session_id = (
        f"session_{hashlib.sha256(response.json()['command_id'].encode('utf-8')).hexdigest()[:24]}"
    )
    asyncio.run(
        seed_complete_product_workspace(
            database_url,
            tenant_id="tenant_yaya",
            actor_id="student_0001",
            session_id=session_id,
        )
    )
    return session_id


def _interaction_payload(session_id: str, *, include_patch: bool) -> dict[str, object]:
    example = AGENT_ROOT / "contracts/examples/product-agent-interaction-page.json"
    raw = example.read_text(encoding="utf-8")
    turn_id = f"turn_{uuid4().hex}"
    replacements = {
        "session_agent_001": session_id,
        "turn_agent_0001": turn_id,
        "patch_water_001": f"patch_{uuid4().hex}",
        "draft_water_001": f"draft_{uuid4().hex}",
    }
    for old, new in replacements.items():
        raw = raw.replace(old, new)
    interaction = json.loads(raw)["value"]["interactions"][0]
    origin = interaction["request_context"]
    feedback = interaction["feedback"]
    feedback_event = interaction["feedback_event"]
    source = interaction["projection_source"]
    assert isinstance(origin, dict)
    assert isinstance(feedback, dict)
    assert isinstance(feedback_event, dict)
    assert isinstance(source, dict)
    origin["requested_at"] = datetime.fromisoformat(
        str(origin["requested_at"]).replace("Z", "+00:00")
    ).isoformat()

    command_id = f"cmd_product_turn_{uuid4().hex}"
    invocation_id = side_effect_execution_id(command_id, turn_id)
    run_id = f"run_{hashlib.sha256(invocation_id.encode('utf-8')).hexdigest()[:24]}"
    event_id = f"evt_product_feedback_{uuid4().hex}"
    job_id = workflow_job_id("tenant_yaya", command_id)
    interaction_id = _scoped_identifier("interaction", "tenant_yaya", job_id)
    interaction.update(
        {
            "interaction_id": interaction_id,
            "session_id": session_id,
            "turn_id": turn_id,
        }
    )
    feedback.update(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "command_id": command_id,
            "run_id": run_id,
        }
    )
    feedback_event.update(
        {
            "event_id": event_id,
            "stream_id": f"agent-session:{session_id}",
            "command_id": command_id,
            "content_ref": interaction["request_context"]["content_ref"],
        }
    )
    feedback_event["occurred_at"] = (
        datetime.fromisoformat(str(feedback_event["occurred_at"]).replace("Z", "+00:00"))
        .isoformat()
        .replace("+00:00", "Z")
    )
    source.update(
        {
            "receipt_id": workflow_step_receipt_id("tenant_yaya", job_id, "TURN_COMPLETED"),
            "actor": interaction["request_context"]["actor"],
            "content_ref": interaction["request_context"]["content_ref"],
            "interaction_id": interaction_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "command_id": command_id,
            "feedback_event_id": event_id,
            "committed_at": interaction["created_at"],
        }
    )
    links = interaction["links"]
    assert isinstance(links, dict)
    links.update(
        {
            "self": (
                f"/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}"
            ),
            "session_workspace": (f"/product-experience/v1/sessions/{session_id}/workspace"),
        }
    )
    patch = interaction.get("skill_patch")
    if include_patch:
        assert isinstance(patch, dict)
        patch.update(
            {
                "interaction_id": interaction_id,
                "session_id": session_id,
                "turn_id": turn_id,
            }
        )
        links["skill_draft"] = (
            f"/product-experience/v1/sessions/{session_id}/skill-drafts/{patch['draft_id']}"
        )
    else:
        interaction.update(
            {
                "role": "teaching_agent",
                "response_type": "message",
                "question": None,
                "hint_level": None,
                "skill_patch": None,
            }
        )
        source.update(
            {
                "role": "teaching_agent",
                "response_type": "message",
                "question": None,
                "hint_level": None,
                "skill_patch_sha256": None,
            }
        )
        links["skill_draft"] = None
    _rehash_interaction(interaction)
    return interaction


def _rehash_interaction(interaction: dict[str, object]) -> None:
    feedback = interaction["feedback"]
    feedback_event = interaction["feedback_event"]
    source = interaction["projection_source"]
    patch = interaction.get("skill_patch")
    assert isinstance(feedback, dict)
    assert isinstance(feedback_event, dict)
    assert isinstance(source, dict)
    if isinstance(patch, dict):
        patch_without_hash = {key: value for key, value in patch.items() if key != "patch_sha256"}
        patch["patch_sha256"] = hashlib.sha256(canonical_payload(patch_without_hash)).hexdigest()
        source["skill_patch_sha256"] = patch["patch_sha256"]
    else:
        source["skill_patch_sha256"] = None
    feedback_sha256 = hashlib.sha256(canonical_payload(feedback)).hexdigest()
    feedback_event["feedback_sha256"] = feedback_sha256
    source["feedback_sha256"] = feedback_sha256
    source_without_hash = {key: value for key, value in source.items() if key != "source_sha256"}
    source["source_sha256"] = hashlib.sha256(canonical_payload(source_without_hash)).hexdigest()


def _record(client: TestClient, interaction: dict[str, object]) -> None:
    context = OperationContext(
        request_id="req_product_interaction_seed",
        correlation_id="corr_product_interaction_seed",
        trace_id="trace_product_interaction_seed",
        requested_at=datetime.now(UTC),
        actor=ActorRef("tenant_yaya", "student_0001", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef("YAYA_FARM_001", "1.4.0", "a" * 64),
        schema_version="1.0.0",
        command_id=f"cmd_{uuid4().hex}",
        causation_id=None,
    )
    # Run on TestClient's lifespan loop: asyncpg connections are loop-affine.
    assert client.portal is not None
    app = cast(FastAPI, client.app)
    client.portal.call(
        _seed_interaction_authority,
        app.state.product_interactions._store._sessions,
        interaction,
    )
    outcome = client.portal.call(app.state.product_interactions._store.record, interaction, context)
    assert outcome.__class__.__name__ == "Success", outcome
    client.portal.call(
        _refresh_workspace,
        app.state.product_interactions._store._sessions,
        str(interaction["session_id"]),
    )


async def _seed_interaction_authority(
    sessions: async_sessionmaker[AsyncSession],
    interaction: dict[str, object],
) -> None:
    origin = interaction["request_context"]
    feedback = interaction["feedback"]
    feedback_event = interaction["feedback_event"]
    source = interaction["projection_source"]
    assert isinstance(origin, dict)
    assert isinstance(feedback, dict)
    assert isinstance(feedback_event, dict)
    assert isinstance(source, dict)
    session_id = str(interaction["session_id"])
    turn_id = str(interaction["turn_id"])
    command_id = str(feedback["command_id"])
    run_id = str(feedback["run_id"])
    created_at = datetime.fromisoformat(str(interaction["created_at"]).replace("Z", "+00:00"))
    accepted_at = datetime.fromisoformat(str(origin["requested_at"]).replace("Z", "+00:00"))
    feedback_at = datetime.fromisoformat(str(feedback["completed_at"]).replace("Z", "+00:00"))
    event_at = datetime.fromisoformat(str(feedback_event["occurred_at"]).replace("Z", "+00:00"))
    job_id = workflow_job_id("tenant_yaya", command_id)
    invocation_id = side_effect_execution_id(command_id, turn_id)
    skill_suffix = hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:24]
    skill = {
        "skill_id": f"skill_{skill_suffix}",
        "skill_version_id": f"skillver_{skill_suffix}",
        "artifact_sha256": "d" * 64,
        "certification_id": f"cert_{skill_suffix}",
    }
    turn_request = {
        "turn_id": turn_id,
        "expected_world_revision": 0,
        "input": {"type": "MESSAGE", "text": "water the plot", "locale": "zh-CN"},
        "skill_bindings": [skill],
        "client_state": {"last_event_sequence": 0, "client_turn_sequence": 1},
    }
    accepted_request_sha256 = hashlib.sha256(canonical_payload(turn_request)).hexdigest()
    arguments = {"action": "water"}

    async with sessions() as session, session.begin():
        owner = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == "tenant_yaya",
                AgentSessionRow.actor_id == "student_0001",
                AgentSessionRow.session_id == session_id,
            )
            .with_for_update()
        )
        assert owner is not None
        invocation_request_sha256 = skill_invocation_request_sha256(
            tenant_id="tenant_yaya",
            invocation_id=invocation_id,
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            world_id=owner.world_id,
            expected_world_revision=0,
            skill_ref=SkillRef(**skill),
            arguments=arguments,
        )
        assert accepted_request_sha256 != invocation_request_sha256
        command = command_record_from_data(
            {
                "request_context": origin,
                "command_id": command_id,
                "revision": 2,
                "command_type": "EXECUTE_AGENT_TURN",
                "status": "REJECTED",
                "stage": "WORLD_VALIDATE",
                "terminal": True,
                "accepted_at": accepted_at.isoformat().replace("+00:00", "Z"),
                "updated_at": created_at.isoformat().replace("+00:00", "Z"),
                "result": None,
                "error": {
                    "code": "WORLD_RULE_REJECTED",
                    "category": "WORLD_RULE",
                    "retryable": False,
                    "user_message_key": "world.rule_rejected",
                    "stage": "WORLD_VALIDATE",
                    "message": "The staged actions did not satisfy the activated World rules.",
                    "details": {
                        "reason_code": "TASK_INCOMPLETE",
                        "evidence_ids": [
                            item["evidence_id"]
                            for item in cast(list[dict[str, object]], feedback["evidence_refs"])
                        ],
                    },
                    "evidence_ids": [],
                },
                "evidence_refs": feedback["evidence_refs"],
                "versions": owner.session_json["versions"],
                "links": {
                    "self": f"/v1/commands/{command_id}",
                    "run": f"/v1/runs/{run_id}",
                    "world_snapshot": f"/v1/worlds/{owner.world_id}/snapshot",
                },
            }
        )
        event_json = dict(feedback_event)
        event_json.pop("feedback_sha256")
        # ProductInteraction is a public projection and uses canonical UTC `Z`.
        # The durable EventRow retains the original domain-event wire spelling.
        event_json["occurred_at"] = event_at.isoformat()
        event_json["payload"] = feedback
        run_json = {
            "request_context": origin,
            "run_id": run_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "command_id": command_id,
            "status": "REJECTED",
            "terminal": True,
            "skill": skill,
            "sandbox": {"invocation_id": invocation_id},
            "world_application": {"status": "REJECTED", "receipt": None, "failure": {}},
            "created_at": feedback["completed_at"],
            "updated_at": feedback["completed_at"],
            "evidence_refs": feedback["evidence_refs"],
            "agent_feedback": feedback,
        }
        invocation_result = {
            "schema_version": "1.0.0",
            "invocation_id": invocation_id,
            "tenant_id": "tenant_yaya",
            "request_sha256": invocation_request_sha256,
            "arguments": arguments,
            "run": {
                "run_id": run_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "command_id": command_id,
                "world_id": owner.world_id,
                "skill_ref": skill,
                "task_success": False,
                "world_revision_before": 0,
                "world_revision_after": 0,
                "world_difference": {},
                "failed_actions": [],
                "failure_key": "task_incomplete",
                "evidence_refs": feedback["evidence_refs"],
                "world_commit": None,
                "request_context": origin,
            },
        }
        session.add_all(
            [
                CommandRow(
                    command_id=command.command_id,
                    tenant_id="tenant_yaya",
                    actor_id="student_0001",
                    command_type=command.command_type,
                    status=command.status.value,
                    revision=command.revision,
                    terminal=command.terminal,
                    accepted_at=command.accepted_at,
                    updated_at=command.updated_at,
                    record_json=command_record_data(command),
                ),
                AgentTurnRow(
                    tenant_id="tenant_yaya",
                    actor_id="student_0001",
                    session_id=session_id,
                    turn_id=turn_id,
                    command_id=command_id,
                    turn_sequence=1,
                    created_at=accepted_at,
                    request_json=turn_request,
                ),
                IdempotencyReceiptRow(
                    tenant_id="tenant_yaya",
                    actor_id="student_0001",
                    operation="EXECUTE_AGENT_TURN",
                    idempotency_key=f"idem_product_turn_{skill_suffix}",
                    request_sha256=accepted_request_sha256,
                    command_id=command_id,
                    accepted_at=accepted_at,
                ),
                RunRow(
                    run_id=run_id,
                    tenant_id="tenant_yaya",
                    actor_id="student_0001",
                    content_hash="a" * 64,
                    session_id=session_id,
                    turn_id=turn_id,
                    command_id=command_id,
                    created_at=feedback_at,
                    run_json=run_json,
                ),
                EventRow(
                    event_id=str(feedback_event["event_id"]),
                    tenant_id="tenant_yaya",
                    stream_id=str(feedback_event["stream_id"]),
                    sequence=int(feedback_event["sequence"]),
                    occurred_at=event_at,
                    event_json=event_json,
                ),
                WorkflowJobRow(
                    job_id=job_id,
                    tenant_id="tenant_yaya",
                    command_id=command_id,
                    operation="EXECUTE_AGENT_TURN",
                    subject_type="AGENT_TURN",
                    subject_id=turn_id,
                    phase="COMPLETE",
                    status="SUCCEEDED",
                    attempt=1,
                    fencing_token=2,
                    lease_owner=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    request_sha256=accepted_request_sha256,
                    job_json={
                        "schema_version": "1.0.0",
                        "request_context": origin,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "turn_sequence": 1,
                        "request": turn_request,
                    },
                    last_error_json=None,
                    created_at=accepted_at,
                    updated_at=created_at,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                JobStepReceiptRow(
                    receipt_id=workflow_step_receipt_id("tenant_yaya", job_id, "SKILL_INVOKED"),
                    tenant_id="tenant_yaya",
                    job_id=job_id,
                    step_name="SKILL_INVOKED",
                    fencing_token=1,
                    input_sha256=invocation_request_sha256,
                    output_sha256=workflow_receipt_sha256(invocation_result),
                    receipt_json=invocation_result,
                    completed_at=feedback_at,
                ),
                JobStepReceiptRow(
                    receipt_id=str(source["receipt_id"]),
                    tenant_id="tenant_yaya",
                    job_id=job_id,
                    step_name="TURN_COMPLETED",
                    fencing_token=2,
                    input_sha256=invocation_request_sha256,
                    output_sha256=workflow_receipt_sha256(source),
                    receipt_json=source,
                    completed_at=created_at,
                ),
            ]
        )


async def _refresh_workspace(
    sessions: async_sessionmaker[AsyncSession],
    session_id: str,
) -> None:
    async with sessions() as session, session.begin():
        workspace = await session.scalar(
            select(ProductWorkspaceRow)
            .where(
                ProductWorkspaceRow.tenant_id == "tenant_yaya",
                ProductWorkspaceRow.actor_id == "student_0001",
                ProductWorkspaceRow.session_id == session_id,
            )
            .with_for_update()
        )
        database_now = await session.scalar(select(func.clock_timestamp()))
        assert workspace is not None
        assert isinstance(database_now, datetime) and database_now.tzinfo is not None
        updated_at = max(database_now.astimezone(UTC), workspace.updated_at.astimezone(UTC))
        await refresh_workspace_in_session(
            session,
            tenant_id="tenant_yaya",
            actor_id="student_0001",
            session_id=session_id,
            updated_at=updated_at,
        )


def _assert_interaction_reads_rejected(
    client: TestClient,
    session_id: str,
    interaction_id: str,
    headers: dict[str, str],
) -> None:
    item = client.get(
        f"/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}",
        headers=headers,
    )
    assert item.status_code != 200, item.text
    assert item.json()["error"]["code"] == "INVARIANT_VIOLATION"
    page = client.get(
        f"/product-experience/v1/sessions/{session_id}/agent-interactions",
        headers=headers,
    )
    assert page.status_code != 200, page.text
    assert page.json()["error"]["code"] == "INVARIANT_VIOLATION"


async def _replace_interaction_json(
    sessions: async_sessionmaker[AsyncSession],
    interaction_id: str,
    value: dict[str, object],
) -> None:
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(ProductInteractionRow)
            .where(ProductInteractionRow.interaction_id == interaction_id)
            .with_for_update()
        )
        assert row is not None
        row.interaction_json = copy.deepcopy(value)


async def _read_event_json(
    sessions: async_sessionmaker[AsyncSession], event_id: str
) -> dict[str, object]:
    async with sessions() as session:
        row = await session.scalar(select(EventRow).where(EventRow.event_id == event_id))
        assert row is not None
        return copy.deepcopy(row.event_json)


async def _replace_event_json(
    sessions: async_sessionmaker[AsyncSession],
    event_id: str,
    value: dict[str, object],
) -> None:
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(EventRow).where(EventRow.event_id == event_id).with_for_update()
        )
        assert row is not None
        row.event_json = copy.deepcopy(value)


async def _read_command_json(
    sessions: async_sessionmaker[AsyncSession], command_id: str
) -> dict[str, object]:
    async with sessions() as session:
        row = await session.scalar(select(CommandRow).where(CommandRow.command_id == command_id))
        assert row is not None
        return copy.deepcopy(row.record_json)


async def _replace_command_json(
    sessions: async_sessionmaker[AsyncSession],
    command_id: str,
    value: dict[str, object],
) -> None:
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == command_id).with_for_update()
        )
        assert row is not None
        row.record_json = copy.deepcopy(value)


async def _read_interaction_receipt_authority(
    sessions: async_sessionmaker[AsyncSession], command_id: str
) -> dict[str, object]:
    async with sessions() as session:
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
        )
        assert job is not None
        receipts = list(
            (
                await session.scalars(
                    select(JobStepReceiptRow).where(
                        JobStepReceiptRow.job_id == job.job_id,
                        JobStepReceiptRow.step_name.in_(("SKILL_INVOKED", "TURN_COMPLETED")),
                    )
                )
            ).all()
        )
        by_step = {receipt.step_name: receipt for receipt in receipts}
        invocation = by_step["SKILL_INVOKED"]
        terminal = by_step["TURN_COMPLETED"]
        return {
            "job_request_sha256": job.request_sha256,
            "invocation_input_sha256": invocation.input_sha256,
            "invocation_output_sha256": invocation.output_sha256,
            "invocation_request_sha256": invocation.receipt_json["request_sha256"],
            "invocation_receipt_json": copy.deepcopy(invocation.receipt_json),
            "terminal_input_sha256": terminal.input_sha256,
        }


async def _replace_skill_invocation_receipt(
    sessions: async_sessionmaker[AsyncSession],
    command_id: str,
    input_sha256: str,
    output_sha256: str,
    receipt_json: dict[str, object],
) -> None:
    async with sessions() as session, session.begin():
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
        )
        assert job is not None
        receipt = await session.scalar(
            select(JobStepReceiptRow)
            .where(
                JobStepReceiptRow.job_id == job.job_id,
                JobStepReceiptRow.step_name == "SKILL_INVOKED",
            )
            .with_for_update()
        )
        assert receipt is not None
        receipt.input_sha256 = input_sha256
        receipt.output_sha256 = output_sha256
        receipt.receipt_json = copy.deepcopy(receipt_json)


async def _replace_terminal_receipt_input(
    sessions: async_sessionmaker[AsyncSession],
    command_id: str,
    input_sha256: str,
) -> None:
    async with sessions() as session, session.begin():
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
        )
        assert job is not None
        receipt = await session.scalar(
            select(JobStepReceiptRow)
            .where(
                JobStepReceiptRow.job_id == job.job_id,
                JobStepReceiptRow.step_name == "TURN_COMPLETED",
            )
            .with_for_update()
        )
        assert receipt is not None
        receipt.input_sha256 = input_sha256


async def _replace_job_request_sha256(
    sessions: async_sessionmaker[AsyncSession],
    command_id: str,
    request_sha256: str,
) -> None:
    async with sessions() as session, session.begin():
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id).with_for_update()
        )
        assert job is not None
        job.request_sha256 = request_sha256


def _create_draft(client: TestClient, session_id: str) -> tuple[str, dict[str, object]]:
    draft_id = f"draft_{uuid4().hex}"
    skill_id = f"skill_{uuid4().hex}"
    source = "int main() { return 0; }\n"
    response = client.put(
        f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}",
        headers={**HEADERS, "Idempotency-Key": f"idem_draft_{uuid4().hex}"},
        json={
            "session_id": session_id,
            "draft_id": draft_id,
            "skill_id": skill_id,
            "content_ref": {
                "unit_id": "YAYA_FARM_001",
                "version": "1.4.0",
                "content_hash": "a" * 64,
            },
            "base_revision": 0,
            "base_draft_sha256": None,
            "display_name": "Original skill",
            "source_bundle": {
                "language": "CPP20",
                "entrypoint": "main.cpp",
                "files": [
                    {
                        "path": "main.cpp",
                        "content": source,
                        "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    }
                ],
            },
            "client_saved_at": "2026-08-10T01:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    return draft_id, response.json()


def _draft_hash(draft: dict[str, object], display_name: str) -> str:
    projection = {
        "session_id": draft["session_id"],
        "draft_id": draft["draft_id"],
        "skill_id": draft["skill_id"],
        "content_ref": draft["content_ref"],
        "display_name": display_name,
        "source_bundle": draft["source_bundle"],
    }
    return hashlib.sha256(canonical_payload(projection)).hexdigest()


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "\x00".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(framed).hexdigest()[:24]}"
