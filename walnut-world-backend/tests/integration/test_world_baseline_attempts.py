"""Real PostgreSQL coverage for scoring a Run against the level baseline.

A Run is one attempt at a level, not a move in an accumulating game. Watering
adds to hydration and success compares hydration against exact expected units,
so applying each Run to the previous result meant:

  * a correct program passed exactly once and never again -- the World was left
    sitting on the answer, and replaying it overshot; and
  * a single overshooting Run made the level permanently unreachable, because
    hydration only ever grows.

Either way the student was locked out of a level they could no longer finish,
with no reset anywhere in the product. Recording the level's starting World on
the snapshot and applying every Run to it makes attempts independent.

Worlds seeded before the baseline existed carry none, and those must keep the
original accumulating behaviour -- pinned by the last test here.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    OperationContext,
    RequestContext,
    SkillRef,
    Success,
    UncommittedEvent,
    WaterIntent,
    WorldAtomicCommit,
    WorldCommand,
    WorldSnapshot,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres.models import (
    WorldSnapshotRow,
    world_snapshot_data,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.world import (
    PostgresWorldUnitOfWork,
    world_commit_identifier,
)
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules

EXPECTED_UNITS = (2, 1, 0)
PLOT_COUNT = len(EXPECTED_UNITS)


def _database_url() -> str:
    url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL world coverage")
    return url


def _ruleset() -> WorldRules:
    return WorldRules(
        content_version="1.0.0",
        max_actions=8,
        min_x=0,
        max_x=8,
        min_y=0,
        max_y=8,
        harvest_growth_stage=2,
        success_score=PLOT_COUNT,
        watering_expected_units=EXPECTED_UNITS,
    )


def _baseline_state() -> dict[str, Any]:
    return {
        "clock": {"day": 1, "minute_of_day": 0, "tick": 1},
        "avatar": {"entity_id": "avatar_0001", "position": {"x": 0, "y": 0}, "energy": 100},
        "inventory": [],
        "plots": [
            {
                "plot_id": f"plot_{index:04d}",
                "position": {"x": index, "y": 0},
                "soil_state": "TILLED",
                "hydration": 0,
                "crop": {
                    "crop_type": "tomato",
                    "growth_stage": 2,
                    "planted_at_tick": 1,
                    "ready_to_harvest": True,
                },
                "last_updated_event_sequence": 0,
            }
            for index in range(1, PLOT_COUNT + 1)
        ],
        "agents": [],
    }


def _context(run_id: str) -> OperationContext:
    return OperationContext(
        request_id=f"req_{run_id}",
        correlation_id=f"corr_{run_id}",
        trace_id=f"trace_{run_id}",
        requested_at=datetime.now(UTC),
        actor=ActorRef(
            tenant_id=f"tenant_{run_id}",
            actor_id=f"actor_{run_id}",
            actor_type=ActorType.STUDENT,
        ),
        content_ref=ContentRef(unit_id="UNIT_TEST", version="1.0.0", content_hash="0" * 64),
        command_id=f"cmd_{run_id}",
        causation_id=None,
    )


def _watering_intents(amounts: tuple[int, ...], revision: int) -> tuple[WaterIntent, ...]:
    return tuple(
        WaterIntent(
            f"intent_water_{index + 1:04d}",
            "avatar_0001",
            revision,
            f"plot_{index + 1:04d}",
            amount,
        )
        for index, amount in enumerate(amounts)
        if amount > 0
    )


def _commit(
    run_id: str,
    context: OperationContext,
    world_id: str,
    *,
    amounts: tuple[int, ...],
    revision: int,
    sequence: int,
    baseline: dict[str, Any],
) -> WorldAtomicCommit:
    intents = _watering_intents(amounts, revision)
    command = WorldCommand(
        run_id=f"run_{run_id}_{revision}",
        world_id=world_id,
        expected_world_revision=revision,
        world_rules_version="rules-1",
        skill_ref=SkillRef(
            skill_id="skill_0001",
            skill_version_id="skill_version_0001",
            artifact_sha256="1" * 64,
            certification_id="cert_0001",
        ),
        intents=intents,
    )
    # The commit's own event must carry the hash the unit of work will compute,
    # and that is now derived from the baseline rather than the carried state.
    transition = WorldEngine().apply(baseline, intents, _ruleset())
    event = UncommittedEvent(
        event_type="world.committed",
        event_version=1,
        producer="walnut_world_engine",
        trace_id=context.trace_id,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        causation_id=context.command_id,
        content_ref=context.content_ref,
        payload={
            "commit_id": world_commit_identifier(
                context.actor.tenant_id, f"world:{world_id}", command.run_id, revision
            ),
            "run_id": command.run_id,
            "world_id": world_id,
            "previous_world_revision": revision,
            "world_revision": revision + 1,
            "state_hash": transition.state_hash,
            "applied_intent_ids": transition.applied_intent_ids,
            "committed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "evidence_refs": (),
        },
    )
    return WorldAtomicCommit(
        stream_id=f"world:{world_id}",
        expected_stream_sequence="NO_STREAM" if sequence == 0 else sequence,
        command=command,
        events=(event,),
        outbox_messages=(),
    )


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
    context: OperationContext,
    world_id: str,
    *,
    with_baseline: bool,
) -> None:
    state = _baseline_state()
    snapshot = WorldSnapshot(
        request_context=RequestContext(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            requested_at=context.requested_at,
            actor=context.actor,
            content_ref=context.content_ref,
        ),
        world_id=world_id,
        revision=0,
        last_event_sequence=0,
        state_hash=canonical_json_sha256(state),
        generated_at=context.requested_at,
        world_rules_version="rules-1",
        state=state,
    )
    value = world_snapshot_data(snapshot)
    if with_baseline:
        value["baseline_state"] = _baseline_state()
    async with sessions() as session, session.begin():
        session.add(
            WorldSnapshotRow(
                world_id=world_id,
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                content_hash=context.content_ref.content_hash,
                revision=0,
                last_event_sequence=0,
                state_hash=snapshot.state_hash,
                generated_at=context.requested_at,
                snapshot_json=value,
            )
        )


async def _current_state(
    sessions: async_sessionmaker[AsyncSession], world_id: str
) -> dict[str, Any]:
    async with sessions() as session:
        row = await session.scalar(
            select(WorldSnapshotRow).where(WorldSnapshotRow.world_id == world_id)
        )
        assert row is not None
        return dict(row.snapshot_json["state"])


async def _hydration(
    sessions: async_sessionmaker[AsyncSession], world_id: str
) -> tuple[int, ...]:
    async with sessions() as session:
        row = await session.scalar(
            select(WorldSnapshotRow).where(WorldSnapshotRow.world_id == world_id)
        )
        assert row is not None
        plots = row.snapshot_json["state"]["plots"]
        return tuple(int(plot["hydration"]) for plot in plots)


async def _exercise(*, with_baseline: bool, first: tuple[int, ...]) -> tuple[int, ...]:
    """Run `first`, then the correct answer, and report the resulting hydration."""

    sessions = create_session_factory(_database_url())
    world = PostgresWorldUnitOfWork(sessions, {"rules-1": _ruleset()})
    run_id = uuid4().hex
    context = _context(run_id)
    world_id = f"world_{run_id}"
    try:
        await _seed(sessions, context, world_id, with_baseline=with_baseline)
        baseline = _baseline_state()
        applied = await world.commit(
            _commit(
                run_id, context, world_id,
                amounts=first, revision=0, sequence=0, baseline=baseline,
            ),
            context,
        )
        assert isinstance(applied, Success), applied
        # The second attempt is the correct answer, submitted after the first.
        # Its event must carry the hash the unit of work will compute: from the
        # baseline when one exists, otherwise from whatever the first Run left
        # behind, which is read back rather than reconstructed.
        carried = baseline if with_baseline else await _current_state(sessions, world_id)
        second = await world.commit(
            _commit(
                run_id, context, world_id,
                amounts=EXPECTED_UNITS, revision=1, sequence=1, baseline=carried,
            ),
            context,
        )
        assert isinstance(second, Success), second
        return await _hydration(sessions, world_id)
    finally:
        await sessions.kw["bind"].dispose()


def test_a_correct_run_can_be_repeated_after_it_already_passed() -> None:
    # First attempt is already correct, so the World ends on the answer. Without
    # a baseline the replay would double every plot and the level could never be
    # completed again.
    result = asyncio.run(_exercise(with_baseline=True, first=EXPECTED_UNITS))
    assert result == EXPECTED_UNITS


def test_a_level_survives_an_overshooting_run() -> None:
    # The student floods every plot, then submits the correct answer. Hydration
    # only grows, so without a baseline the expected units are unreachable for
    # the rest of the session.
    result = asyncio.run(_exercise(with_baseline=True, first=(9, 9, 0)))
    assert result == EXPECTED_UNITS


def test_worlds_without_a_baseline_keep_accumulating() -> None:
    # The compatibility boundary: worlds seeded before the baseline existed must
    # behave exactly as before, so this one still doubles.
    result = asyncio.run(_exercise(with_baseline=False, first=EXPECTED_UNITS))
    assert result == tuple(unit * 2 for unit in EXPECTED_UNITS)
