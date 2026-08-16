"""Released Game read operationIds against durable PostgreSQL World data."""

from __future__ import annotations

import asyncio
import copy
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    FrozenJsonObject,
    MoveIntent,
    OperationContext,
    RequestContext,
    SkillRef,
    UncommittedEvent,
    WorldAtomicCommit,
    WorldCommand,
    WorldPosition,
    WorldSnapshot,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres.models import WorldSnapshotRow, world_snapshot_data
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.world import PostgresWorldUnitOfWork, world_commit_identifier
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEADERS = {
    "Authorization": "Bearer tenant_yaya:student_actor",
    "X-Request-Id": "req_game_reads_0001",
    "X-Trace-Id": "trace_game_reads_0001",
    "X-Correlation-Id": "corr_game_reads_0001",
    "X-Schema-Version": "1.0.0",
}


def test_world_read_operations_are_contract_valid_and_actor_scoped() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL Game read coverage"
        )
    asyncio.run(_exercise_reads(database_url))


async def _exercise_reads(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    context = operation_context()
    request = make_commit(context)
    try:
        await seed_snapshot(sessions, request, context)
        committed = await PostgresWorldUnitOfWork(sessions, {"rules-1": ruleset()}).commit(
            request, context
        )
        assert committed.ok
        settings = replace(
            Settings.for_test(
                contract_path=DEFAULT_CONTRACT_PATH,
                contract_release_path=BACKEND_ROOT / "contract-release.json",
            ),
            database_url=database_url,
        )
        with TestClient(create_app(settings)) as client:
            bootstrap = client.get("/v1/bootstrap", headers=HEADERS)
            assert bootstrap.status_code == 200
            assert bootstrap.json()["world"]["world_id"] == request.command.world_id
            assert bootstrap.json()["capabilities"]["world_event_stream"] is False
            assert bootstrap.json()["capabilities"]["client_event_batch"] is False
            assert bootstrap.json()["world"]["stream_url"] == "wss://localhost/v1/realtime"

            snapshot = client.get(
                f"/v1/worlds/{request.command.world_id}/snapshot", headers=HEADERS
            )
            assert snapshot.status_code == 200
            assert snapshot.headers["x-world-revision"] == "1"
            assert snapshot.headers["etag"] == (
                f'"{request.command.world_id}:1:{snapshot.json()["state_hash"]}"'
            )
            snapshot_body = snapshot.json()

            original_state = await _replace_snapshot_state(
                sessions,
                request.command.world_id,
                {"corrupt": "state no longer matches the retained state_hash"},
            )
            try:
                corrupt_snapshot = client.get(
                    f"/v1/worlds/{request.command.world_id}/snapshot", headers=HEADERS
                )
                assert corrupt_snapshot.status_code == 500, corrupt_snapshot.text
                assert corrupt_snapshot.json()["error"]["code"] == "INVARIANT_VIOLATION"
            finally:
                await _replace_snapshot_state(
                    sessions,
                    request.command.world_id,
                    original_state,
                )

            restored_snapshot = client.get(
                f"/v1/worlds/{request.command.world_id}/snapshot", headers=HEADERS
            )
            assert restored_snapshot.status_code == 200, restored_snapshot.text
            assert restored_snapshot.json() == snapshot_body

            events = client.get(
                f"/v1/worlds/{request.command.world_id}/events?after_sequence=0", headers=HEADERS
            )
            assert events.status_code == 200
            page = events.json()
            assert page["from_sequence"] == page["to_sequence"] == 1
            assert page["next_after_sequence"] == 1
            assert page["events"][0]["event_type"] == "world.committed"
            assert "state" not in page["events"][0]["payload"]

            denied = client.get(
                f"/v1/worlds/{request.command.world_id}/snapshot",
                headers={**HEADERS, "Authorization": "Bearer tenant_yaya:student_other"},
            )
            assert denied.status_code == 404
    finally:
        await sessions.kw["bind"].dispose()


def operation_context() -> OperationContext:
    return OperationContext(
        request_id="req_game_reads_seed_0001",
        correlation_id="corr_game_reads_seed_0001",
        trace_id="trace_game_reads_seed_0001",
        requested_at=datetime.now(UTC),
        actor=ActorRef("tenant_yaya", "student_actor", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef("UNIT_TRANSPORT", "1.0.0", "0" * 64),
        command_id="cmd_game_reads_seed_0001",
        causation_id=None,
    )


def make_commit(context: OperationContext) -> WorldAtomicCommit:
    world_id = f"world_{uuid4().hex}"
    state = snapshot_state()
    command = WorldCommand(
        run_id=f"run_{uuid4().hex}",
        world_id=world_id,
        expected_world_revision=0,
        world_rules_version="rules-1",
        skill_ref=SkillRef("skill_0001", "skill_version_0001", "1" * 64, "cert_0001"),
        intents=(MoveIntent("intent_move_001", "avatar_0001", 0, WorldPosition(2, 1)),),
    )
    transition = WorldEngine().apply(state, command.intents, ruleset())
    committed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return WorldAtomicCommit(
        stream_id=f"world:{world_id}",
        expected_stream_sequence="NO_STREAM",
        command=command,
        events=(
            UncommittedEvent(
                event_type="world.committed",
                event_version=1,
                producer="world-engine",
                trace_id=context.trace_id,
                command_id=context.command_id,
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
                content_ref=context.content_ref,
                payload={
                    "commit_id": world_commit_identifier(
                        context.actor.tenant_id, f"world:{world_id}", command.run_id, 0
                    ),
                    "run_id": command.run_id,
                    "world_id": world_id,
                    "previous_world_revision": 0,
                    "world_revision": 1,
                    "state_hash": transition.state_hash,
                    "applied_intent_ids": ("intent_move_001",),
                    "committed_at": committed_at,
                    "evidence_refs": (),
                },
            ),
        ),
        outbox_messages=(),
    )


async def seed_snapshot(
    sessions: async_sessionmaker[AsyncSession],
    request: WorldAtomicCommit,
    context: OperationContext,
) -> None:
    state = snapshot_state()
    snapshot = WorldSnapshot(
        request_context=RequestContext(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            requested_at=context.requested_at,
            actor=context.actor,
            content_ref=context.content_ref,
        ),
        world_id=request.command.world_id,
        revision=0,
        last_event_sequence=0,
        state_hash=canonical_json_sha256(state),
        generated_at=context.requested_at,
        world_rules_version="rules-1",
        state=cast(FrozenJsonObject, state),
    )
    async with sessions() as session, session.begin():
        session.add(
            WorldSnapshotRow(
                world_id=snapshot.world_id,
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                content_hash=context.content_ref.content_hash,
                revision=0,
                last_event_sequence=0,
                state_hash=snapshot.state_hash,
                generated_at=snapshot.generated_at,
                snapshot_json=world_snapshot_data(snapshot),
            )
        )


async def _replace_snapshot_state(
    sessions: async_sessionmaker[AsyncSession],
    world_id: str,
    replacement: dict[str, object],
) -> dict[str, object]:
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(WorldSnapshotRow).where(WorldSnapshotRow.world_id == world_id).with_for_update()
        )
        assert row is not None
        snapshot_json = copy.deepcopy(row.snapshot_json)
        original = copy.deepcopy(snapshot_json["state"])
        assert isinstance(original, dict)
        snapshot_json["state"] = replacement
        row.snapshot_json = snapshot_json
        return cast(dict[str, object], original)


def snapshot_state() -> dict[str, object]:
    return {
        "clock": {"day": 1, "minute_of_day": 0, "tick": 1},
        "avatar": {
            "entity_id": "avatar_0001",
            "position": {"x": 1, "y": 1},
            "energy": 100,
        },
        "inventory": [],
        "plots": [],
        "agents": [],
    }


def ruleset() -> WorldRules:
    return WorldRules("1.0.0", 4, 0, 4, 0, 4, 2, 1)
