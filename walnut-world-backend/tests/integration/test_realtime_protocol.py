"""The public WSS replays only committed World events in sequence order."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from tests.contract.test_game_read_operations import (
    make_commit,
    operation_context,
    ruleset,
    seed_snapshot,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.world import PostgresWorldUnitOfWork
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_actor",
    "X-Request-Id": "req_realtime_0001",
    "X-Trace-Id": "trace_realtime_0001",
    "X-Correlation-Id": "corr_realtime_0001",
    "X-Schema-Version": "1.0.0",
    "X-Stream-Protocol-Version": "1.0.0",
}
RUNTIME_SUBPROTOCOL = "yaya.runtime.v1"


def test_realtime_subscription_replays_committed_events_and_requires_contiguous_ack() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required WSS PostgreSQL coverage")
    asyncio.run(_exercise_realtime(database_url))


async def _exercise_realtime(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    context = operation_context()
    commit = make_commit(context)
    try:
        await seed_snapshot(sessions, commit, context)
        result = await PostgresWorldUnitOfWork(sessions, {"rules-1": ruleset()}).commit(commit, context)
        assert result.ok
        settings = replace(
            Settings.for_test(
                contract_path=DEFAULT_CONTRACT_PATH,
                contract_release_path=BACKEND_ROOT / "contract-release.json",
            ),
            database_url=database_url,
            realtime_wss_enabled=True,
        )
        with TestClient(create_app(settings)) as client:
            with client.websocket_connect(
                "/v1/realtime", headers=HEADERS, subprotocols=[RUNTIME_SUBPROTOCOL]
            ) as websocket:
                websocket.send_json(
                    {
                        "frame_type": "subscribe",
                        "protocol_version": "1.0.0",
                        "request_id": "req_realtime_subscribe_0001",
                        "stream_id": commit.stream_id,
                        "after_sequence": 0,
                    }
                )
                subscribed = websocket.receive_json()
                assert subscribed["frame_type"] == "subscribed"
                assert subscribed["accepted_after_sequence"] == 0
                assert subscribed["high_watermark_sequence"] == 1
                event = websocket.receive_json()
                assert event["event_type"] == "world.committed"
                websocket.send_json(
                    {
                        "frame_type": "ack",
                        "protocol_version": "1.0.0",
                        "subscription_id": subscribed["subscription_id"],
                        "stream_id": commit.stream_id,
                        "sequence": event["sequence"],
                        "event_id": event["event_id"],
                    }
                )
                heartbeat = websocket.receive_json()
                assert heartbeat["frame_type"] == "heartbeat"
                websocket.send_json(
                    {
                        "frame_type": "heartbeat_ack",
                        "protocol_version": "1.0.0",
                        "subscription_id": subscribed["subscription_id"],
                        "nonce": heartbeat["nonce"],
                        "received_at": "2026-08-09T12:00:00Z",
                    }
                )
    finally:
        await sessions.kw["bind"].dispose()
