"""Client outbox batches are accepted atomically and replay by command key."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    CommandRecord,
    CommandStatus,
    CommandTransition,
    ContentRef,
    Failure,
    OperationContext,
    Result,
)

from tests.integration._session_authority_support import seed_session_launch_authority
from walnut_backend.api import middleware as transport_middleware
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

AGENT_ROOT = DEFAULT_CONTRACT_PATH
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_events",
    "X-Request-Id": "req_client_event_0001",
    "X-Trace-Id": "trace_client_event_0001",
    "X-Correlation-Id": "corr_client_event_0001",
    "X-Schema-Version": "1.0.0",
}


def test_client_event_batch_accepts_and_replays(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for Client Event PostgreSQL coverage")
    settings = replace(
        Settings.for_test(contract_path=AGENT_ROOT),
        database_url=database_url,
        client_event_batch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        session_id, world_id = _session(client, database_url)
        host_requested_at = datetime.now(UTC) + timedelta(seconds=30)

        class _AheadMiddlewareDateTime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                assert tz is UTC
                return host_requested_at

        monkeypatch.setattr(transport_middleware, "datetime", _AheadMiddlewareDateTime)
        transitions: list[CommandRecord] = []
        command_store = client.app.state.game_queries._command_store
        original_transition = command_store.transition_in_session

        async def capture_transition(
            session: AsyncSession,
            transition: CommandTransition,
            context: OperationContext,
        ) -> Result[CommandRecord]:
            result = await original_transition(session, transition, context)
            transitions.append(transition.next_record)
            return result

        monkeypatch.setattr(command_store, "transition_in_session", capture_transition)
        event_id = f"client_evt_{uuid4().hex}"
        body = {
            "batch_id": f"batch_{uuid4().hex}",
            "session_id": session_id,
            "world_id": world_id,
            "first_sequence": 1,
            "last_sequence": 1,
            "events": [
                {
                    "event_id": event_id,
                    "sequence": 1,
                    "occurred_at": "2026-08-10T02:00:00Z",
                    "event_type": "SESSION_HEARTBEAT",
                    "world_revision": 0,
                    "payload": {"last_received_event_sequence": 0},
                }
            ],
        }
        headers = {**HEADERS, "Idempotency-Key": f"idem_events_{uuid4().hex}"}
        accepted = client.post("/v1/client-events:batch", headers=headers, json=body)
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers["idempotency-replayed"] == "false"
        command = client.get(accepted.headers["location"], headers=HEADERS)
        assert command.status_code == 200, command.text
        command_payload = command.json()
        assert command_payload["status"] == "APPLIED"
        assert command_payload["result"] == {
            "result_type": "CLIENT_EVENTS_ACCEPTED",
            "batch_id": body["batch_id"],
            "accepted_count": 1,
            "duplicate_count": 0,
            "rejected_count": 0,
        }
        assert [record.status for record in transitions] == [
            CommandStatus.VALIDATING,
            CommandStatus.APPLIED,
        ]
        requested_at = _timestamp(command_payload["request_context"]["requested_at"])
        accepted_at = _timestamp(command_payload["accepted_at"])
        validating_at = transitions[0].updated_at
        completed_at = transitions[1].updated_at
        assert requested_at == host_requested_at == accepted_at
        assert requested_at <= accepted_at <= validating_at <= completed_at
        assert _timestamp(accepted.json()["created_at"]) == accepted_at
        assert _timestamp(accepted.json()["updated_at"]) == accepted_at
        assert _timestamp(command_payload["updated_at"]) == completed_at
        replay = client.post(
            "/v1/client-events:batch",
            headers={
                **headers,
                "X-Request-Id": "req_client_event_0002",
                "X-Trace-Id": "trace_client_event_0002",
                "X-Correlation-Id": "corr_client_event_0002",
            },
            json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json()["command_id"] == accepted.json()["command_id"]
        assert replay.json()["created_at"] == accepted.json()["created_at"]
        assert _timestamp(replay.json()["updated_at"]) == completed_at
        assert len(transitions) == 2
        replayed_command = client.get(replay.headers["location"], headers=HEADERS)
        assert replayed_command.status_code == 200, replayed_command.text
        assert replayed_command.json() == command_payload

        conflicting_key = f"idem_events_conflict_{uuid4().hex}"
        conflict_body = {
            **body,
            "batch_id": f"batch_{uuid4().hex}",
            "events": [{**body["events"][0], "event_id": f"client_evt_{uuid4().hex}"}],
        }
        conflict = client.post(
            "/v1/client-events:batch",
            headers={**HEADERS, "Idempotency-Key": conflicting_key},
            json=conflict_body,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "EVENT_SEQUENCE_GAP"
        assert client.portal is not None
        receipt = client.portal.call(
            client.app.state.game_queries._command_store.get_by_idempotency_key,
            "INGEST_CLIENT_EVENTS",
            conflicting_key,
            _command_context(),
        )
        assert isinstance(receipt, Failure)

        invalid = {**body, "batch_id": f"batch_{uuid4().hex}", "last_sequence": 2}
        invalid_response = client.post(
            "/v1/client-events:batch",
            headers={**HEADERS, "Idempotency-Key": f"idem_events_{uuid4().hex}"},
            json=invalid,
        )
        assert invalid_response.status_code == 409
        assert invalid_response.json()["error"]["code"] == "EVENT_SEQUENCE_GAP"


def _session(client: TestClient, database_url: str) -> tuple[str, str]:
    world_id = "world_event_0001"
    request = {
        "world_id": world_id,
        "learner_id": "learner_event_0001",
        "agent_profile_id": "agent_event_0001",
        "channel": "GAME",
        "locale": "zh-CN",
        "content": {
            "unit_id": "UNIT_EVENT_001",
            "version": "1.0.0",
            "content_hash": "e" * 64,
        },
    }
    asyncio.run(
        seed_session_launch_authority(
            database_url,
            tenant_id="tenant_yaya",
            actor_id="student_events",
            request=request,
        )
    )
    response = client.post(
        "/v1/agent-sessions",
        headers={**HEADERS, "Idempotency-Key": f"idem_event_session_{uuid4().hex}"},
        json=request,
    )
    assert response.status_code == 202, response.text
    command_id = response.json()["command_id"]
    import hashlib

    return f"session_{hashlib.sha256(command_id.encode()).hexdigest()[:24]}", world_id


def _command_context() -> OperationContext:
    return OperationContext(
        request_id="req_client_event_lookup_0001",
        correlation_id="corr_client_event_lookup_0001",
        trace_id="trace_client_event_lookup_0001",
        requested_at=datetime.now(UTC),
        actor=ActorRef("tenant_yaya", "student_events", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef("UNIT_EVENT_001", "1.0.0", "e" * 64),
        schema_version="1.0.0",
        command_id=f"cmd_{uuid4().hex}",
        causation_id=None,
    )


def _timestamp(value: object) -> datetime:
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(UTC)
