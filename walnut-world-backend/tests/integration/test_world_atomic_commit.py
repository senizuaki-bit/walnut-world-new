"""Real PostgreSQL coverage for the one authoritative world write transaction."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    Failure,
    MoveIntent,
    OperationContext,
    RequestContext,
    SkillRef,
    Success,
    UncommittedEvent,
    WorldAtomicCommit,
    WorldCommand,
    WorldPosition,
    WorldSnapshot,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres.event_store import PostgresEventStore
from walnut_backend.adapters.postgres.models import WorldSnapshotRow, world_snapshot_data
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.world import PostgresWorldUnitOfWork, world_commit_identifier
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules


def test_world_commit_is_atomic_and_compare_and_swap_backed() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL world coverage")
    asyncio.run(_exercise_atomic_commit(database_url))


async def _exercise_atomic_commit(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    world = PostgresWorldUnitOfWork(sessions, {"rules-1": ruleset()})
    events = PostgresEventStore(sessions)
    run_id = uuid4().hex
    context = make_context(run_id)
    request = make_commit(run_id, context)
    try:
        await seed_snapshot(sessions, request, context)
        first, second = await asyncio.gather(
            world.commit(request, context), world.commit(request, context)
        )
        successes = [result for result in (first, second) if isinstance(result, Success)]
        failures = [result for result in (first, second) if isinstance(result, Failure)]
        assert len(successes) == 1, [failure.error for failure in failures]
        assert len(failures) == 1
        assert failures[0].error.code == "WORLD_REVISION_CONFLICT"

        receipt = successes[0].value
        assert receipt.world.previous_revision == 0
        assert receipt.world.world_revision == 1
        assert receipt.world.first_event_sequence == receipt.events.previous_sequence + 1
        assert receipt.world.last_event_sequence == receipt.events.next_sequence

        streamed = await events.read_stream(request.stream_id, 0, 10, context)
        assert isinstance(streamed, Success)
        assert [event.sequence for event in streamed.value.items] == [1]
        event = streamed.value.items[0]
        assert event.event_type == "world.committed"
        assert event.payload["state_hash"] == receipt.world.state_hash
        assert "state" not in event.payload

        wrong_stream = WorldAtomicCommit(
            stream_id=f"world:wrong_{run_id}",
            expected_stream_sequence="NO_STREAM",
            command=request.command,
            events=request.events,
            outbox_messages=(),
        )
        rejected = await world.commit(wrong_stream, context)
        assert isinstance(rejected, Failure)
        assert rejected.error.code == "INVARIANT_VIOLATION"
    finally:
        await sessions.kw["bind"].dispose()


def make_commit(run_id: str, context: OperationContext) -> WorldAtomicCommit:
    world_id = f"world_{run_id}"
    state = {
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
    command = WorldCommand(
        run_id=f"run_{run_id}",
        world_id=world_id,
        expected_world_revision=0,
        world_rules_version="rules-1",
        skill_ref=SkillRef(
            skill_id="skill_0001",
            skill_version_id="skill_version_0001",
            artifact_sha256="1" * 64,
            certification_id="cert_0001",
        ),
        intents=(
            MoveIntent("intent_move_001", "avatar_0001", 0, WorldPosition(1, 1)),
        ),
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
                    "previous_world_revision": command.expected_world_revision,
                    "world_revision": 1,
                    "state_hash": transition.state_hash,
                    "applied_intent_ids": tuple(intent.intent_id for intent in command.intents),
                    "committed_at": committed_at,
                    "evidence_refs": (),
                },
            ),
        ),
        outbox_messages=(),
    )


def make_context(run_id: str) -> OperationContext:
    return OperationContext(
        request_id=f"req_{run_id}",
        correlation_id=f"corr_{run_id}",
        trace_id=f"trace_{run_id}",
        requested_at=datetime.now(UTC),
        actor=ActorRef(
            tenant_id=f"tenant_{run_id}", actor_id=f"actor_{run_id}", actor_type=ActorType.STUDENT
        ),
        content_ref=ContentRef(unit_id="UNIT_TEST", version="1.0.0", content_hash="0" * 64),
        command_id=f"cmd_{run_id}",
        causation_id=None,
    )


async def seed_snapshot(
    sessions: async_sessionmaker[AsyncSession], request: WorldAtomicCommit, context: OperationContext
) -> None:
    state = {
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
        world_rules_version=request.command.world_rules_version,
        state=state,
    )
    async with sessions() as session, session.begin():
        session.add(
            WorldSnapshotRow(
                world_id=snapshot.world_id,
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                content_hash=context.content_ref.content_hash,
                revision=snapshot.revision,
                last_event_sequence=snapshot.last_event_sequence,
                state_hash=snapshot.state_hash,
                generated_at=snapshot.generated_at,
                snapshot_json=world_snapshot_data(snapshot),
            )
        )


def ruleset() -> WorldRules:
    return WorldRules(
        content_version="1.0.0",
        max_actions=4,
        min_x=0,
        max_x=4,
        min_y=0,
        max_y=4,
        harvest_growth_stage=2,
        success_score=1,
    )
