"""PostgreSQL atomicity for the INT2 authoritative presentation projection."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    DeliveryPayload,
    Failure,
    FeishuReportDraftBody,
    HarvestIntent,
    MoveIntent,
    OperationContext,
    OutboxMessage,
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

from walnut_backend.adapters.postgres.models import (
    AgentTurnRow,
    EventRow,
    OutboxRow,
    WorldPresentationEventRow,
    WorldPresentationStreamRow,
    WorldSnapshotRow,
    world_snapshot_data,
    world_snapshot_from_data,
)
from walnut_backend.adapters.postgres.outbox import PostgresOutbox
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.world import PostgresWorldUnitOfWork, world_commit_identifier
from walnut_backend.adapters.postgres.world_presentation import (
    PostgresWorldPresentation,
    presentation_integrity_sha256,
)
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, ContractRelease, Settings
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules


def test_harvest_projection_and_gap_marker_are_atomic() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL coverage")
    asyncio.run(_exercise_projection(database_url))


def test_formal_eight_harvest_commit_returns_one_closed_page() -> None:
    """The real student journey commits eight actions, not the two-action fixture."""

    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL coverage")
    asyncio.run(_exercise_eight_harvest_projection(database_url))


def test_world_presentation_snapshot_event_and_outbox_roll_back_together() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL coverage")
    asyncio.run(_exercise_projection_rollback(database_url))


def test_world_presentation_commit_retry_after_ack_loss_is_side_effect_free() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL coverage")
    asyncio.run(_exercise_projection_ack_loss(database_url))


def test_presentation_read_uses_durable_content_after_actor_authentication() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for required PostgreSQL coverage")
    asyncio.run(_exercise_transport_placeholder_content(database_url))


async def _exercise_projection(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        harvest_context = _context("harvest")
        harvest_request = _request(harvest_context, mixed=False)
        await _seed(sessions, harvest_request, harvest_context)
        committed = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=2)}
        ).commit(harvest_request, harvest_context)
        assert isinstance(committed, Success)

        followup_context = _followup_context(harvest_context)
        async with sessions() as session, session.begin():
            current_row = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == harvest_context.actor.tenant_id,
                    WorldSnapshotRow.world_id == harvest_request.command.world_id,
                )
            )
            assert current_row is not None
            current_snapshot = world_snapshot_from_data(current_row.snapshot_json)
            session.add(
                AgentTurnRow(
                    tenant_id=followup_context.actor.tenant_id,
                    actor_id=followup_context.actor.actor_id,
                    session_id=f"session_{followup_context.command_id}",
                    turn_id=f"turn_{followup_context.command_id}",
                    command_id=followup_context.command_id,
                    turn_sequence=2,
                    created_at=followup_context.requested_at,
                    request_json={"test": "second presentation commit"},
                )
            )
        followup_request = _followup_request(
            followup_context, current_snapshot, harvest_request.command.world_id
        )
        followup = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=2)}
        ).commit(followup_request, followup_context)
        assert isinstance(followup, Success)

        async with sessions() as session:
            head = await session.scalar(
                select(WorldPresentationStreamRow).where(
                    WorldPresentationStreamRow.tenant_id == harvest_context.actor.tenant_id,
                    WorldPresentationStreamRow.world_id == harvest_request.command.world_id,
                )
            )
            assert head is not None
            assert head.stream_id == f"world-presentation:{harvest_request.command.world_id}"
            assert head.last_sequence == 4
            assert head.last_world_revision == 2
            assert head.last_world_event_sequence == 2
            assert head.last_snapshot_state_hash == followup.value.world.state_hash
            assert head.gap_world_revision is None
            rows = tuple(
                (
                    await session.scalars(
                        select(WorldPresentationEventRow)
                        .where(
                            WorldPresentationEventRow.tenant_id
                            == harvest_context.actor.tenant_id,
                            WorldPresentationEventRow.stream_id == head.stream_id,
                        )
                        .order_by(WorldPresentationEventRow.sequence)
                    )
                ).all()
            )
            assert [row.sequence for row in rows] == [1, 2, 3, 4]
            assert [row.event_json["action_index"] for row in rows] == [0, 1, 0, 1]
            assert rows[0].event_json["state_hash_after"] == rows[1].event_json["state_hash_before"]
            assert rows[1].event_json["state_hash_after"] == committed.value.world.state_hash
            assert rows[2].event_json["state_hash_before"] == committed.value.world.state_hash
            assert rows[3].event_json["state_hash_after"] == followup.value.world.state_hash
            assert all(row.event_json["event_type"] == "world.action.harvested" for row in rows)

        async with sessions() as session:
            final_row = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == harvest_context.actor.tenant_id,
                    WorldSnapshotRow.world_id == harvest_request.command.world_id,
                )
            )
            assert final_row is not None
            final_snapshot = world_snapshot_from_data(final_row.snapshot_json)
        reader = PostgresWorldPresentation(sessions)
        cold_page = await reader.list_page(final_snapshot, 0, 1, followup_context)
        assert isinstance(cold_page, Success)
        assert cold_page.value["presentation_high_watermark"] == 4
        assert cold_page.value["from_sequence"] == cold_page.value["to_sequence"] == 1
        assert cold_page.value["events"][0]["action_index"] == 0
        assert cold_page.value["has_more"] is True
        mid_commit_page = await reader.list_page(final_snapshot, 1, 1, followup_context)
        assert isinstance(mid_commit_page, Success)
        assert mid_commit_page.value["from_sequence"] == mid_commit_page.value["to_sequence"] == 2
        assert mid_commit_page.value["events"][0]["action_index"] == 1
        first_page = await reader.list_page(final_snapshot, 0, 2, followup_context)
        assert isinstance(first_page, Success)
        assert first_page.value["from_sequence"] == 1
        assert first_page.value["to_sequence"] == 2
        assert first_page.value["has_more"] is True
        assert first_page.value["events"][-1]["final_snapshot_revision"] == 1
        assert first_page.value["snapshot_revision"] == 2
        second_page = await reader.list_page(final_snapshot, 2, 2, followup_context)
        assert isinstance(second_page, Success)
        assert second_page.value["from_sequence"] == 3
        assert second_page.value["to_sequence"] == 4
        assert second_page.value["has_more"] is False
        assert second_page.value["events"][-1]["final_snapshot_revision"] == 2
        empty_page = await reader.list_page(final_snapshot, 4, 2, followup_context)
        assert isinstance(empty_page, Success)
        assert empty_page.value["from_sequence"] == 4
        assert empty_page.value["to_sequence"] == 4
        assert empty_page.value["next_after_sequence"] == 4

        async with sessions() as session, session.begin():
            corrupt = await session.scalar(
                select(WorldPresentationEventRow).where(
                    WorldPresentationEventRow.tenant_id == harvest_context.actor.tenant_id,
                    WorldPresentationEventRow.sequence == 2,
                )
            )
            assert corrupt is not None
            retained = dict(corrupt.event_json)
            damaged = dict(retained)
            damaged["payload_sha256"] = "f" * 64
            corrupt.event_json = damaged
        rejected = await reader.list_page(final_snapshot, 0, 4, followup_context)
        assert isinstance(rejected, Failure)
        assert rejected.error.code == "EVENT_SEQUENCE_GAP"
        async with sessions() as session, session.begin():
            corrupt = await session.scalar(
                select(WorldPresentationEventRow).where(
                    WorldPresentationEventRow.tenant_id == harvest_context.actor.tenant_id,
                    WorldPresentationEventRow.sequence == 2,
                )
            )
            assert corrupt is not None
            corrupt.event_json = retained

        # A corrupt writer could consistently rewrite both retained JSON and
        # mirrored columns, including the event identity/hash.  Historical
        # commit bindings still have to advance strictly at commit boundaries;
        # equality with the following commit is not a valid history.
        async with sessions() as session, session.begin():
            first_commit = tuple(
                (
                    await session.scalars(
                        select(WorldPresentationEventRow)
                        .where(
                            WorldPresentationEventRow.tenant_id
                            == harvest_context.actor.tenant_id,
                            WorldPresentationEventRow.world_revision == 1,
                        )
                        .order_by(WorldPresentationEventRow.action_index)
                    )
                ).all()
            )
            assert len(first_commit) == 2
            retained_commit = [_presentation_row_copy(row) for row in first_commit]
            for row in first_commit:
                _rewrite_final_world_event_sequence(row, 2)
        monotonic_rejected = await reader.list_page(
            final_snapshot, 0, 4, followup_context
        )
        assert isinstance(monotonic_rejected, Failure)
        assert monotonic_rejected.error.code == "EVENT_SEQUENCE_GAP"
        async with sessions() as session, session.begin():
            first_commit = tuple(
                (
                    await session.scalars(
                        select(WorldPresentationEventRow)
                        .where(
                            WorldPresentationEventRow.tenant_id
                            == harvest_context.actor.tenant_id,
                            WorldPresentationEventRow.world_revision == 1,
                        )
                        .order_by(WorldPresentationEventRow.action_index)
                    )
                ).all()
            )
            for row, retained_row in zip(first_commit, retained_commit, strict=True):
                _restore_presentation_row(row, retained_row)

        mixed_context = _context("mixed")
        mixed_request = _request(mixed_context, mixed=True)
        await _seed(sessions, mixed_request, mixed_context)
        mixed = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=1)}
        ).commit(mixed_request, mixed_context)
        assert isinstance(mixed, Success)
        async with sessions() as session:
            head = await session.scalar(
                select(WorldPresentationStreamRow).where(
                    WorldPresentationStreamRow.tenant_id == mixed_context.actor.tenant_id,
                    WorldPresentationStreamRow.world_id == mixed_request.command.world_id,
                )
            )
            assert head is not None
            assert head.last_sequence == 0
            assert head.gap_world_revision == 1
            count = await session.scalar(
                select(func.count())
                .select_from(WorldPresentationEventRow)
                .where(WorldPresentationEventRow.tenant_id == mixed_context.actor.tenant_id)
            )
            assert count == 0
        async with sessions() as session:
            mixed_snapshot_row = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == mixed_context.actor.tenant_id,
                    WorldSnapshotRow.world_id == mixed_request.command.world_id,
                )
            )
            assert mixed_snapshot_row is not None
            mixed_snapshot = world_snapshot_from_data(mixed_snapshot_row.snapshot_json)
        gap = await PostgresWorldPresentation(sessions).list_page(
            mixed_snapshot, 0, 100, mixed_context
        )
        assert isinstance(gap, Failure)
        assert gap.error.code == "EVENT_SEQUENCE_GAP"
    finally:
        await sessions.kw["bind"].dispose()


async def _exercise_transport_placeholder_content(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    durable_context = _context("transport_placeholder")
    durable_context = OperationContext(
        request_id=durable_context.request_id,
        correlation_id=durable_context.correlation_id,
        trace_id=durable_context.trace_id,
        requested_at=durable_context.requested_at,
        actor=durable_context.actor,
        content_ref=ContentRef("UNIT_INT2", "1.0.0", "b" * 64),
        command_id=durable_context.command_id,
        causation_id=durable_context.causation_id,
    )
    request = _request(durable_context, mixed=False)
    try:
        await _seed(sessions, request, durable_context)
        committed = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=2)}
        ).commit(request, durable_context)
        assert isinstance(committed, Success)
        async with sessions() as session:
            snapshot_row = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == durable_context.actor.tenant_id,
                    WorldSnapshotRow.world_id == request.command.world_id,
                )
            )
            assert snapshot_row is not None
            snapshot = world_snapshot_from_data(snapshot_row.snapshot_json)

        # GET middleware authenticates the actor but intentionally has only a
        # transport placeholder for content.  It must neither select nor replace
        # the durable Snapshot content authority.
        transport_context = OperationContext(
            request_id="req_transport_placeholder_read",
            correlation_id=durable_context.correlation_id,
            trace_id="trace_transport_placeholder_read",
            requested_at=datetime.now(UTC),
            actor=durable_context.actor,
            content_ref=ContentRef("UNIT_TRANSPORT", "1.0.0", "0" * 64),
            command_id="cmd_transport_placeholder_read",
            causation_id=None,
        )
        reader = PostgresWorldPresentation(sessions)
        page = await reader.list_page(snapshot, 0, 100, transport_context)
        assert isinstance(page, Success), getattr(page, "error", None)
        assert page.value["request_context"]["content_ref"]["content_hash"] == "b" * 64

        wrong_actor = OperationContext(
            request_id=transport_context.request_id,
            correlation_id=transport_context.correlation_id,
            trace_id=transport_context.trace_id,
            requested_at=transport_context.requested_at,
            actor=ActorRef(
                tenant_id=durable_context.actor.tenant_id,
                actor_id=f"{durable_context.actor.actor_id}_other",
                actor_type=ActorType.STUDENT,
                roles=("game:player",),
            ),
            content_ref=transport_context.content_ref,
            command_id=transport_context.command_id,
            causation_id=None,
        )
        unauthorized = await reader.list_page(snapshot, 0, 100, wrong_actor)
        assert isinstance(unauthorized, Failure)
        assert unauthorized.error.code == "EVENT_SEQUENCE_GAP"

        async with sessions() as session, session.begin():
            event = await session.scalar(
                select(WorldPresentationEventRow).where(
                    WorldPresentationEventRow.tenant_id == durable_context.actor.tenant_id,
                    WorldPresentationEventRow.world_id == request.command.world_id,
                    WorldPresentationEventRow.sequence == 1,
                )
            )
            assert event is not None
            event.content_hash = "c" * 64
        corrupt = await reader.list_page(snapshot, 0, 100, transport_context)
        assert isinstance(corrupt, Failure)
        assert corrupt.error.code == "EVENT_SEQUENCE_GAP"
    finally:
        await sessions.kw["bind"].dispose()


async def _exercise_eight_harvest_projection(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    context = _context("formal_eight_harvest")
    state = _state_with_plots(8)
    world_id = f"world_{context.command_id}"
    run_id = f"run_{context.command_id}"
    intents = tuple(
        HarvestIntent(
            f"intent_harvest_{index:04d}",
            "avatar_0001",
            0,
            f"plot_{index:04d}",
        )
        for index in range(1, 9)
    )
    command = WorldCommand(
        run_id=run_id,
        world_id=world_id,
        expected_world_revision=0,
        world_rules_version="rules-1",
        skill_ref=SkillRef("skill_0001", "skill_version_0001", "1" * 64, "cert_0001"),
        intents=intents,
    )
    transition = WorldEngine().apply(state, intents, _rules(success_score=8))
    committed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    request = WorldAtomicCommit(
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
                        context.actor.tenant_id, f"world:{world_id}", run_id, 0
                    ),
                    "run_id": run_id,
                    "world_id": world_id,
                    "previous_world_revision": 0,
                    "world_revision": 1,
                    "state_hash": transition.state_hash,
                    "applied_intent_ids": transition.applied_intent_ids,
                    "committed_at": committed_at,
                    "evidence_refs": (),
                },
            ),
        ),
        outbox_messages=(),
    )
    try:
        await _seed_state(sessions, request, context, state)
        committed = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=8)}
        ).commit(request, context)
        assert isinstance(committed, Success)
        async with sessions() as session:
            snapshot_row = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.world_id == world_id,
                )
            )
            assert snapshot_row is not None
            snapshot = world_snapshot_from_data(snapshot_row.snapshot_json)
        page = await PostgresWorldPresentation(sessions).list_page(
            snapshot, 0, 100, context
        )
        assert isinstance(page, Success), getattr(page, "error", None)
        assert page.value["presentation_high_watermark"] == 8
        assert len(page.value["events"]) == 8
        assert [event["action_index"] for event in page.value["events"]] == list(range(8))
        assert page.value["events"][-1]["state_hash_after"] == committed.value.world.state_hash
        schema_errors = ContractRelease(
            Settings.for_test(
                contract_path=DEFAULT_CONTRACT_PATH,
                contract_release_path=Path(__file__).resolve().parents[2]
                / "contract-release.json",
            )
        ).validate(
            "contracts/schemas/game/world-presentation-event-page.schema.json",
            page.value,
        )
        assert schema_errors == []
    finally:
        await sessions.kw["bind"].dispose()


async def _exercise_projection_rollback(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    context = _context("presentation_rollback")
    request = _request(context, mixed=False)
    delivery_key = f"presentation-rollback-{uuid4().hex}"
    retained = _outbox_message(context, delivery_key, "retained")
    conflict = _outbox_message(context, delivery_key, "conflict")
    try:
        await _seed(sessions, request, context)
        seeded = await PostgresOutbox(sessions).enqueue(retained, context)
        assert isinstance(seeded, Success)
        rejected = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=2)}
        ).commit(_with_outbox(request, conflict), context)
        assert isinstance(rejected, Failure)
        assert rejected.error.code == "INVARIANT_VIOLATION"
        assert rejected.error.message is not None
        assert "outbox message conflicts" in rejected.error.message

        async with sessions() as session:
            snapshot = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.world_id == request.command.world_id,
                )
            )
            assert snapshot is not None
            world_events = await session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(
                    EventRow.tenant_id == context.actor.tenant_id,
                    EventRow.stream_id == request.stream_id,
                )
            )
            presentation_heads = await session.scalar(
                select(func.count())
                .select_from(WorldPresentationStreamRow)
                .where(WorldPresentationStreamRow.tenant_id == context.actor.tenant_id)
            )
            presentation_events = await session.scalar(
                select(func.count())
                .select_from(WorldPresentationEventRow)
                .where(WorldPresentationEventRow.tenant_id == context.actor.tenant_id)
            )
            outbox_rows = await session.scalar(
                select(func.count())
                .select_from(OutboxRow)
                .where(OutboxRow.tenant_id == context.actor.tenant_id)
            )
        assert snapshot.revision == 0
        assert snapshot.last_event_sequence == 0
        assert int(world_events or 0) == 0
        assert int(presentation_heads or 0) == 0
        assert int(presentation_events or 0) == 0
        assert int(outbox_rows or 0) == 1
    finally:
        await sessions.kw["bind"].dispose()


async def _exercise_projection_ack_loss(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    context = _context("presentation_ack_loss")
    base_request = _request(context, mixed=False)
    request = _with_outbox(
        base_request,
        _outbox_message(context, f"presentation-ack-{uuid4().hex}", "committed"),
    )
    try:
        await _seed(sessions, request, context)
        committed = await PostgresWorldUnitOfWork(
            sessions, {"rules-1": _rules(success_score=2)}
        ).commit(request, context)
        assert isinstance(committed, Success)
    finally:
        # A new engine/session factory models recovery after the caller lost the
        # COMMIT acknowledgement and both the API and Worker processes restarted.
        await sessions.kw["bind"].dispose()

    recovered_sessions = create_session_factory(database_url)
    try:
        replay = await PostgresWorldUnitOfWork(
            recovered_sessions, {"rules-1": _rules(success_score=2)}
        ).commit(request, context)
        assert isinstance(replay, Failure)
        assert replay.error.code == "WORLD_REVISION_CONFLICT"

        async with recovered_sessions() as session:
            snapshot = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == context.actor.tenant_id,
                    WorldSnapshotRow.world_id == request.command.world_id,
                )
            )
            world_events = await session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(
                    EventRow.tenant_id == context.actor.tenant_id,
                    EventRow.stream_id == request.stream_id,
                )
            )
            head = await session.scalar(
                select(WorldPresentationStreamRow).where(
                    WorldPresentationStreamRow.tenant_id == context.actor.tenant_id,
                    WorldPresentationStreamRow.world_id == request.command.world_id,
                )
            )
            presentation_events = await session.scalar(
                select(func.count())
                .select_from(WorldPresentationEventRow)
                .where(WorldPresentationEventRow.tenant_id == context.actor.tenant_id)
            )
            outbox_rows = await session.scalar(
                select(func.count())
                .select_from(OutboxRow)
                .where(OutboxRow.tenant_id == context.actor.tenant_id)
            )
        assert snapshot is not None
        assert head is not None
        assert snapshot.revision == 1
        assert snapshot.last_event_sequence == 1
        assert snapshot.state_hash == committed.value.world.state_hash
        assert head.last_sequence == 2
        assert head.last_world_revision == 1
        assert head.last_world_event_sequence == 1
        assert int(world_events or 0) == 1
        assert int(presentation_events or 0) == 2
        assert int(outbox_rows or 0) == 1
    finally:
        await recovered_sessions.kw["bind"].dispose()


def _request(context: OperationContext, *, mixed: bool) -> WorldAtomicCommit:
    state = _state()
    world_id = f"world_{context.command_id}"
    run_id = f"run_{context.command_id}"
    intents = (
        (
            HarvestIntent("intent_harvest_0001", "avatar_0001", 0, "plot_0001"),
            MoveIntent("intent_move_0001", "avatar_0001", 0, WorldPosition(3, 1)),
        )
        if mixed
        else (
            HarvestIntent("intent_harvest_0001", "avatar_0001", 0, "plot_0001"),
            HarvestIntent("intent_harvest_0002", "avatar_0001", 0, "plot_0002"),
        )
    )
    command = WorldCommand(
        run_id=run_id,
        world_id=world_id,
        expected_world_revision=0,
        world_rules_version="rules-1",
        skill_ref=SkillRef("skill_0001", "skill_version_0001", "1" * 64, "cert_0001"),
        intents=intents,
    )
    transition = WorldEngine().apply(state, intents, _rules(success_score=1 if mixed else 2))
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
                        context.actor.tenant_id, f"world:{world_id}", run_id, 0
                    ),
                    "run_id": run_id,
                    "world_id": world_id,
                    "previous_world_revision": 0,
                    "world_revision": 1,
                    "state_hash": transition.state_hash,
                    "applied_intent_ids": transition.applied_intent_ids,
                    "committed_at": committed_at,
                    "evidence_refs": (),
                },
            ),
        ),
        outbox_messages=(),
    )


def _context(label: str) -> OperationContext:
    suffix = uuid4().hex
    return OperationContext(
        request_id=f"req_{label}_{suffix}",
        correlation_id=f"corr_{label}_{suffix}",
        trace_id=f"trace_{label}_{suffix}",
        requested_at=datetime.now(UTC),
        actor=ActorRef(
            tenant_id=f"tenant_{label}_{suffix}",
            actor_id=f"actor_{label}_{suffix}",
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        ),
        content_ref=ContentRef("UNIT_INT2", "1.0.0", "0" * 64),
        command_id=f"cmd_{label}_{suffix}",
        causation_id=None,
    )


def _followup_context(first: OperationContext) -> OperationContext:
    suffix = uuid4().hex
    return OperationContext(
        request_id=f"req_followup_{suffix}",
        correlation_id=first.correlation_id,
        trace_id=f"trace_followup_{suffix}",
        requested_at=datetime.now(UTC),
        actor=first.actor,
        content_ref=first.content_ref,
        command_id=f"cmd_followup_{suffix}",
        causation_id=first.command_id,
    )


def _followup_request(
    context: OperationContext, snapshot: WorldSnapshot, world_id: str
) -> WorldAtomicCommit:
    run_id = f"run_{context.command_id}"
    intents = (
        HarvestIntent("intent_harvest_0003", "avatar_0001", 1, "plot_0003"),
        HarvestIntent("intent_harvest_0004", "avatar_0001", 1, "plot_0004"),
    )
    command = WorldCommand(
        run_id=run_id,
        world_id=world_id,
        expected_world_revision=1,
        world_rules_version="rules-1",
        skill_ref=SkillRef("skill_0001", "skill_version_0001", "1" * 64, "cert_0001"),
        intents=intents,
    )
    transition = WorldEngine().apply(snapshot.state, intents, _rules(success_score=2))
    committed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return WorldAtomicCommit(
        stream_id=f"world:{world_id}",
        expected_stream_sequence=1,
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
                        context.actor.tenant_id, f"world:{world_id}", run_id, 1
                    ),
                    "run_id": run_id,
                    "world_id": world_id,
                    "previous_world_revision": 1,
                    "world_revision": 2,
                    "state_hash": transition.state_hash,
                    "applied_intent_ids": transition.applied_intent_ids,
                    "committed_at": committed_at,
                    "evidence_refs": (),
                },
            ),
        ),
        outbox_messages=(),
    )


def _with_outbox(
    request: WorldAtomicCommit, message: OutboxMessage
) -> WorldAtomicCommit:
    return WorldAtomicCommit(
        stream_id=request.stream_id,
        expected_stream_sequence=request.expected_stream_sequence,
        command=request.command,
        events=request.events,
        outbox_messages=(message,),
    )


def _outbox_message(
    context: OperationContext, idempotency_key: str, label: str
) -> OutboxMessage:
    message_id = f"outbox_{label}_{uuid4().hex}"
    return OutboxMessage(
        message_id=message_id,
        destination="FEISHU_REPORT_DRAFT",
        idempotency_key=idempotency_key,
        payload=DeliveryPayload(
            delivery_id=message_id,
            operation="FEISHU_REPORT_DRAFT",
            deduplication_key=idempotency_key,
            attempt=1,
            body=FeishuReportDraftBody(report_id=f"report_{label}_{uuid4().hex}"),
        ),
        created_at=datetime.now(UTC),
        operation_context=context,
    )


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
    request: WorldAtomicCommit,
    context: OperationContext,
) -> None:
    await _seed_state(sessions, request, context, _state())


async def _seed_state(
    sessions: async_sessionmaker[AsyncSession],
    request: WorldAtomicCommit,
    context: OperationContext,
    state: dict[str, object],
) -> None:
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
        state=state,
    )
    async with sessions() as session, session.begin():
        session.add_all(
            [
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
                ),
                AgentTurnRow(
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    session_id=f"session_{context.command_id}",
                    turn_id=f"turn_{context.command_id}",
                    command_id=context.command_id,
                    turn_sequence=1,
                    created_at=context.requested_at,
                    request_json={"test": "presentation identity authority"},
                ),
            ]
        )


def _state() -> dict[str, object]:
    return _state_with_plots(4)


def _state_with_plots(plot_count: int) -> dict[str, object]:
    return {
        "clock": {"day": 1, "minute_of_day": 480, "tick": 10},
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
                    "planted_at_tick": 10,
                    "ready_to_harvest": True,
                },
                "last_updated_event_sequence": 0,
            }
            for index in range(1, plot_count + 1)
        ],
        "agents": [],
    }


def _rules(*, success_score: int) -> WorldRules:
    return WorldRules("1.0.0", 8, 0, 31, 0, 31, 2, success_score)


def _presentation_row_copy(row: WorldPresentationEventRow) -> dict[str, object]:
    return {
        "event_id": row.event_id,
        "final_world_event_sequence": row.final_world_event_sequence,
        "integrity_sha256": row.integrity_sha256,
        "event_json": dict(row.event_json),
    }


def _rewrite_final_world_event_sequence(
    row: WorldPresentationEventRow, sequence: int
) -> None:
    event = dict(row.event_json)
    event["final_world_event_sequence"] = sequence
    event["integrity_sha256"] = presentation_integrity_sha256(event)
    event["event_id"] = f"presentation_{event['integrity_sha256'][:32]}"
    row.event_id = str(event["event_id"])
    row.final_world_event_sequence = sequence
    row.integrity_sha256 = str(event["integrity_sha256"])
    row.event_json = event


def _restore_presentation_row(
    row: WorldPresentationEventRow, retained: dict[str, object]
) -> None:
    row.event_id = str(retained["event_id"])
    row.final_world_event_sequence = int(retained["final_world_event_sequence"])
    row.integrity_sha256 = str(retained["integrity_sha256"])
    retained_json = retained["event_json"]
    assert isinstance(retained_json, dict)
    row.event_json = retained_json
