"""Authoritative INT2 World presentation projection and corruption gates."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    Failure,
    OperationContext,
    Result,
    Success,
    WorldSnapshot,
    canonical_json_sha256,
)

from walnut_backend.domain.world.engine import WorldReducerStep, WorldTransition

from .models import (
    AgentTurnRow,
    WorldPresentationEventRow,
    WorldPresentationStreamRow,
    request_context_data,
)

logger = logging.getLogger(__name__)

EVENT_TYPE = "world.action.harvested"
EVENT_VERSION = 1
SCHEMA_VERSION = "1.0.0"
PRODUCER = "walnut_world_engine"

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_COMMAND_ID = re.compile(r"^cmd_[A-Za-z0-9_-]{8,96}$")
_CROP_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "event_type",
        "event_version",
        "schema_version",
        "stream_id",
        "sequence",
        "occurred_at",
        "producer",
        "tenant_id",
        "session_id",
        "turn_id",
        "command_id",
        "run_id",
        "world_id",
        "commit_id",
        "world_revision",
        "action_index",
        "action_count",
        "intent_id",
        "state_hash_before",
        "state_hash_after",
        "final_snapshot_revision",
        "final_world_event_sequence",
        "final_snapshot_state_hash",
        "payload",
        "payload_sha256",
        "integrity_sha256",
    }
)
_PAYLOAD_KEYS = frozenset(
    {
        "actor_entity_id",
        "plot_id",
        "position",
        "crop_type",
        "growth_stage",
        "ready_to_harvest",
    }
)


class PostgresWorldPresentation:
    """Strict read adapter for the independent authoritative display stream."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def list_page(
        self,
        snapshot: WorldSnapshot,
        after_sequence: int,
        limit: int,
        context: OperationContext,
    ) -> Result[Mapping[str, Any]]:
        try:
            async with self._sessions() as session:
                head = await session.scalar(
                    select(WorldPresentationStreamRow).where(
                        WorldPresentationStreamRow.tenant_id == context.actor.tenant_id,
                        WorldPresentationStreamRow.world_id == snapshot.world_id,
                    )
                )
                if head is None:
                    if snapshot.revision != 0 or snapshot.last_event_sequence != 0:
                        raise ValueError(
                            "committed World has no durable presentation high-watermark"
                        )
                    return Success(_empty_page(snapshot, after_sequence, 0))
                _validate_head(head, snapshot, context)
                if head.gap_world_revision is not None:
                    raise ValueError(
                        "World presentation contains an unsupported committed revision"
                    )
                if after_sequence > head.last_sequence:
                    raise ValueError(
                        "after_sequence is above the presentation high-watermark"
                    )
                if after_sequence == head.last_sequence:
                    return Success(_empty_page(snapshot, after_sequence, head.last_sequence))
                previous = None
                if after_sequence > 0:
                    previous = await session.scalar(
                        select(WorldPresentationEventRow).where(
                            WorldPresentationEventRow.tenant_id == context.actor.tenant_id,
                            WorldPresentationEventRow.stream_id == head.stream_id,
                            WorldPresentationEventRow.sequence == after_sequence,
                        )
                    )
                    if previous is None:
                        raise ValueError("presentation cursor does not name a durable event")
                rows = tuple(
                    (
                        await session.scalars(
                            select(WorldPresentationEventRow)
                            .where(
                                WorldPresentationEventRow.tenant_id
                                == context.actor.tenant_id,
                                WorldPresentationEventRow.stream_id == head.stream_id,
                                WorldPresentationEventRow.sequence > after_sequence,
                            )
                            .order_by(WorldPresentationEventRow.sequence)
                            .limit(limit)
                        )
                    ).all()
                )
                commit_ids = {row.commit_id for row in rows}
                group_rows = tuple(
                    (
                        await session.scalars(
                            select(WorldPresentationEventRow)
                            .where(
                                WorldPresentationEventRow.tenant_id
                                == context.actor.tenant_id,
                                WorldPresentationEventRow.stream_id == head.stream_id,
                                WorldPresentationEventRow.commit_id.in_(commit_ids),
                            )
                            .order_by(
                                WorldPresentationEventRow.world_revision,
                                WorldPresentationEventRow.action_index,
                            )
                        )
                    ).all()
                )
            expected_count = min(limit, head.last_sequence - after_sequence)
            if len(rows) != expected_count:
                raise ValueError("presentation page differs from its durable high-watermark")
            serialized = [
                _validate_event_row(row, snapshot=snapshot, context=context) for row in rows
            ]
            previous_event = (
                _validate_event_row(previous, snapshot=snapshot, context=context)
                if previous is not None
                else None
            )
            commit_groups: dict[str, list[dict[str, Any]]] = {}
            for group_row in group_rows:
                group_event = _validate_event_row(
                    group_row, snapshot=snapshot, context=context
                )
                commit_groups.setdefault(str(group_event["commit_id"]), []).append(group_event)
            _validate_page_events(
                serialized,
                previous_event=previous_event,
                commit_groups=commit_groups,
                after_sequence=after_sequence,
                high_watermark=head.last_sequence,
                initial_world_revision=head.initial_world_revision,
                initial_snapshot_state_hash=head.initial_snapshot_state_hash,
                snapshot=snapshot,
                tenant_id=context.actor.tenant_id,
            )
            from_sequence = int(serialized[0]["sequence"])
            to_sequence = int(serialized[-1]["sequence"])
            return Success(
                {
                    "request_context": request_context_data(snapshot.request_context),
                    "world_id": snapshot.world_id,
                    "snapshot_revision": snapshot.revision,
                    "snapshot_last_event_sequence": snapshot.last_event_sequence,
                    "snapshot_state_hash": snapshot.state_hash,
                    "presentation_high_watermark": head.last_sequence,
                    "from_sequence": from_sequence,
                    "to_sequence": to_sequence,
                    "has_more": to_sequence < head.last_sequence,
                    "next_after_sequence": to_sequence,
                    "events": serialized,
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(
                "world presentation read failed closed: %s",
                error,
            )
            return Failure(_event_gap(str(error)))


async def stage_world_presentation(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    content_hash: str,
    command_id: str,
    run_id: str,
    world_id: str,
    commit_id: str,
    previous_world_revision: int,
    previous_world_event_sequence: int,
    previous_snapshot_state_hash: str,
    world_revision: int,
    world_event_sequence: int,
    final_snapshot_state_hash: str,
    occurred_at: datetime,
    transition: WorldTransition,
) -> None:
    """Stage one complete revision or a permanent gap marker in the caller transaction."""

    stream_id = f"world-presentation:{world_id}"
    head = await session.scalar(
        select(WorldPresentationStreamRow)
        .where(
            WorldPresentationStreamRow.tenant_id == tenant_id,
            WorldPresentationStreamRow.world_id == world_id,
        )
        .with_for_update()
    )
    if head is None:
        head = WorldPresentationStreamRow(
            stream_id=stream_id,
            tenant_id=tenant_id,
            world_id=world_id,
            actor_id=actor_id,
            content_hash=content_hash,
            initial_world_revision=previous_world_revision,
            initial_world_event_sequence=previous_world_event_sequence,
            initial_snapshot_state_hash=previous_snapshot_state_hash,
            last_sequence=0,
            last_world_revision=previous_world_revision,
            last_world_event_sequence=previous_world_event_sequence,
            last_snapshot_state_hash=previous_snapshot_state_hash,
            gap_world_revision=None,
            updated_at=occurred_at,
        )
        session.add(head)
        await session.flush()
    if (
        head.stream_id != stream_id
        or head.actor_id != actor_id
        or head.content_hash != content_hash
        or head.last_world_revision != previous_world_revision
        or head.last_world_event_sequence != previous_world_event_sequence
        or head.last_snapshot_state_hash != previous_snapshot_state_hash
    ):
        raise ValueError("World presentation head differs from the locked World snapshot")

    steps = transition.reducer_steps
    fully_presentable = bool(steps) and all(
        step.action_type == "HARVEST" and step.harvest is not None for step in steps
    )
    if fully_presentable:
        turn = await session.scalar(
            select(AgentTurnRow).where(
                AgentTurnRow.tenant_id == tenant_id,
                AgentTurnRow.actor_id == actor_id,
                AgentTurnRow.command_id == command_id,
            )
        )
        if turn is None:
            raise ValueError("HARVEST presentation has no authoritative Session/Turn identity")
        action_count = len(steps)
        if (
            steps[0].state_hash_before != previous_snapshot_state_hash
            or steps[-1].state_hash_after != final_snapshot_state_hash
            or any(
                left.state_hash_after != right.state_hash_before
                for left, right in zip(steps, steps[1:], strict=False)
            )
        ):
            raise ValueError("World reducer step hashes do not close the committed snapshot")
        for action_index, step in enumerate(steps):
            event = build_harvest_presentation_event(
                step=step,
                stream_id=stream_id,
                sequence=head.last_sequence + action_index + 1,
                occurred_at=occurred_at,
                tenant_id=tenant_id,
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                command_id=command_id,
                run_id=run_id,
                world_id=world_id,
                commit_id=commit_id,
                world_revision=world_revision,
                action_index=action_index,
                action_count=action_count,
                final_world_event_sequence=world_event_sequence,
                final_snapshot_state_hash=final_snapshot_state_hash,
            )
            session.add(_event_row(event, actor_id=actor_id, content_hash=content_hash))
        head.last_sequence += action_count
    elif head.gap_world_revision is None:
        # A revision is all-or-nothing for display. Mixed or unsupported actions
        # keep their valid business commit, but permanently force Snapshot recovery.
        head.gap_world_revision = world_revision

    head.last_world_revision = world_revision
    head.last_world_event_sequence = world_event_sequence
    head.last_snapshot_state_hash = final_snapshot_state_hash
    head.updated_at = occurred_at
    await session.flush()


def build_harvest_presentation_event(
    *,
    step: WorldReducerStep,
    stream_id: str,
    sequence: int,
    occurred_at: datetime,
    tenant_id: str,
    session_id: str,
    turn_id: str,
    command_id: str,
    run_id: str,
    world_id: str,
    commit_id: str,
    world_revision: int,
    action_index: int,
    action_count: int,
    final_world_event_sequence: int,
    final_snapshot_state_hash: str,
) -> dict[str, Any]:
    """Build one stable event solely from a successful authoritative reducer step."""

    harvest = step.harvest
    if step.action_type != "HARVEST" or harvest is None:
        raise ValueError("only an authoritative HARVEST reducer step is presentable")
    payload: dict[str, Any] = {
        "actor_entity_id": harvest.actor_entity_id,
        "plot_id": harvest.plot_id,
        "position": {"x": harvest.position_x, "y": harvest.position_y},
        "crop_type": harvest.crop_type,
        "growth_stage": harvest.growth_stage,
        "ready_to_harvest": harvest.ready_to_harvest,
    }
    event: dict[str, Any] = {
        "event_type": EVENT_TYPE,
        "event_version": EVENT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "stream_id": stream_id,
        "sequence": sequence,
        "occurred_at": _utc_wire(occurred_at),
        "producer": PRODUCER,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "command_id": command_id,
        "run_id": run_id,
        "world_id": world_id,
        "commit_id": commit_id,
        "world_revision": world_revision,
        "action_index": action_index,
        "action_count": action_count,
        "intent_id": step.intent_id,
        "state_hash_before": step.state_hash_before,
        "state_hash_after": step.state_hash_after,
        "final_snapshot_revision": world_revision,
        "final_world_event_sequence": final_world_event_sequence,
        "final_snapshot_state_hash": final_snapshot_state_hash,
        "payload": payload,
        "payload_sha256": canonical_json_sha256(payload),
    }
    event["integrity_sha256"] = presentation_integrity_sha256(event)
    event["event_id"] = f"presentation_{event['integrity_sha256'][:32]}"
    validate_presentation_event_data(event)
    return event


def presentation_integrity_sha256(event: Mapping[str, Any]) -> str:
    """Hash the cross-client fixed-order JSON array used by Godot and Backend.

    The UTF-8 input is compact JSON (no whitespace, JSON booleans, no NaN) in
    this exact order: event_type, event_version, schema_version, stream_id,
    sequence, occurred_at, producer, tenant_id, session_id, turn_id,
    command_id, run_id, world_id, commit_id, world_revision, action_index,
    action_count, intent_id, state_hash_before, state_hash_after,
    final_snapshot_revision, final_world_event_sequence,
    final_snapshot_state_hash, payload_sha256, actor_entity_id, plot_id,
    position.x, position.y, crop_type, growth_stage, ready_to_harvest.
    Payload values are flattened, rather than nested as another array.
    ``event_id`` and ``integrity_sha256`` are derived values and are deliberately
    excluded to avoid a circular digest.
    """

    payload = _mapping(event.get("payload"), "payload")
    payload_values = _payload_integrity_array(payload)
    values = [
        event.get("event_type"),
        event.get("event_version"),
        event.get("schema_version"),
        event.get("stream_id"),
        event.get("sequence"),
        event.get("occurred_at"),
        event.get("producer"),
        event.get("tenant_id"),
        event.get("session_id"),
        event.get("turn_id"),
        event.get("command_id"),
        event.get("run_id"),
        event.get("world_id"),
        event.get("commit_id"),
        event.get("world_revision"),
        event.get("action_index"),
        event.get("action_count"),
        event.get("intent_id"),
        event.get("state_hash_before"),
        event.get("state_hash_after"),
        event.get("final_snapshot_revision"),
        event.get("final_world_event_sequence"),
        event.get("final_snapshot_state_hash"),
        event.get("payload_sha256"),
        *payload_values,
    ]
    return _sha256(values)


def validate_presentation_event_data(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact shape and every self-contained hash; reject all drift."""

    if set(event) != _EVENT_KEYS:
        raise ValueError("presentation event keys differ from the closed wire")
    if (
        event.get("event_type") != EVENT_TYPE
        or event.get("event_version") != EVENT_VERSION
        or event.get("schema_version") != SCHEMA_VERSION
        or event.get("producer") != PRODUCER
    ):
        raise ValueError("presentation event type or version is unsupported")
    identifiers = (
        "tenant_id",
        "session_id",
        "turn_id",
        "run_id",
        "world_id",
        "commit_id",
        "intent_id",
    )
    if any(
        not isinstance(event.get(field), str)
        or _IDENTIFIER.fullmatch(str(event[field])) is None
        for field in identifiers
    ) or _COMMAND_ID.fullmatch(str(event.get("command_id"))) is None:
        raise ValueError("presentation event identity is malformed")
    if event["stream_id"] != f"world-presentation:{event['world_id']}":
        raise ValueError("presentation stream differs from world identity")
    for field in (
        "sequence",
        "world_revision",
        "action_index",
        "action_count",
        "final_snapshot_revision",
        "final_world_event_sequence",
    ):
        if not _is_int(event.get(field)):
            raise ValueError("presentation numeric identity is malformed")
    if (
        event["sequence"] < 1
        or event["world_revision"] < 1
        or event["action_count"] < 1
        or event["action_count"] > 10_000
        or not 0 <= event["action_index"] < event["action_count"]
        or event["final_snapshot_revision"] != event["world_revision"]
        or event["final_world_event_sequence"] < 1
    ):
        raise ValueError("presentation sequence or action index is invalid")
    hashes = (
        "state_hash_before",
        "state_hash_after",
        "final_snapshot_state_hash",
        "payload_sha256",
        "integrity_sha256",
    )
    if any(not _is_sha256(event.get(field)) for field in hashes):
        raise ValueError("presentation hash is malformed")
    if event["state_hash_before"] == event["state_hash_after"]:
        raise ValueError("HARVEST reducer step did not change World state")
    occurred_at = event.get("occurred_at")
    if not isinstance(occurred_at, str):
        raise ValueError("presentation occurred_at is malformed")
    try:
        parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("presentation occurred_at is malformed") from error
    if parsed.tzinfo is None or _utc_wire(parsed) != occurred_at:
        raise ValueError("presentation occurred_at is not canonical UTC")
    payload = _mapping(event.get("payload"), "payload")
    _payload_integrity_array(payload)
    payload_sha256 = canonical_json_sha256(payload)
    if payload_sha256 != event["payload_sha256"]:
        raise ValueError("presentation payload hash differs from payload")
    integrity_sha256 = presentation_integrity_sha256(event)
    if integrity_sha256 != event["integrity_sha256"]:
        raise ValueError("presentation integrity hash differs from event")
    if event.get("event_id") != f"presentation_{integrity_sha256[:32]}":
        raise ValueError("presentation event_id differs from integrity hash")
    return dict(event)


def _event_row(
    event: Mapping[str, Any], *, actor_id: str, content_hash: str
) -> WorldPresentationEventRow:
    occurred_at = datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00"))
    return WorldPresentationEventRow(
        event_id=str(event["event_id"]),
        tenant_id=str(event["tenant_id"]),
        actor_id=actor_id,
        content_hash=content_hash,
        stream_id=str(event["stream_id"]),
        sequence=int(event["sequence"]),
        occurred_at=occurred_at,
        event_type=str(event["event_type"]),
        event_version=int(event["event_version"]),
        schema_version=str(event["schema_version"]),
        producer=str(event["producer"]),
        world_id=str(event["world_id"]),
        session_id=str(event["session_id"]),
        turn_id=str(event["turn_id"]),
        command_id=str(event["command_id"]),
        run_id=str(event["run_id"]),
        commit_id=str(event["commit_id"]),
        world_revision=int(event["world_revision"]),
        action_index=int(event["action_index"]),
        action_count=int(event["action_count"]),
        intent_id=str(event["intent_id"]),
        state_hash_before=str(event["state_hash_before"]),
        state_hash_after=str(event["state_hash_after"]),
        final_snapshot_revision=int(event["final_snapshot_revision"]),
        final_world_event_sequence=int(event["final_world_event_sequence"]),
        final_snapshot_state_hash=str(event["final_snapshot_state_hash"]),
        payload_sha256=str(event["payload_sha256"]),
        integrity_sha256=str(event["integrity_sha256"]),
        event_json=dict(event),
    )


def _validate_head(
    head: WorldPresentationStreamRow,
    snapshot: WorldSnapshot,
    context: OperationContext,
) -> None:
    snapshot_actor = snapshot.request_context.actor
    snapshot_content = snapshot.request_context.content_ref
    if (
        head.stream_id != f"world-presentation:{snapshot.world_id}"
        or head.tenant_id != context.actor.tenant_id
        or head.tenant_id != snapshot_actor.tenant_id
        or head.actor_id != context.actor.actor_id
        or head.actor_id != snapshot_actor.actor_id
        # Public GET middleware has no client-supplied content authority.  Bind
        # the projection to the already validated PostgreSQL Snapshot instead.
        or head.content_hash != snapshot_content.content_hash
        or head.last_sequence < 0
        or head.last_world_revision != snapshot.revision
        or head.last_world_event_sequence != snapshot.last_event_sequence
        or head.last_snapshot_state_hash != snapshot.state_hash
        or not _is_sha256(head.initial_snapshot_state_hash)
    ):
        raise ValueError("presentation high-watermark differs from the World snapshot")


def _validate_event_row(
    row: WorldPresentationEventRow,
    *,
    snapshot: WorldSnapshot,
    context: OperationContext,
) -> dict[str, Any]:
    event = validate_presentation_event_data(row.event_json)
    occurred_at = datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00"))
    snapshot_actor = snapshot.request_context.actor
    snapshot_content = snapshot.request_context.content_ref
    mirrors = {
        "event_id": row.event_id,
        "tenant_id": row.tenant_id,
        "stream_id": row.stream_id,
        "sequence": row.sequence,
        "occurred_at": _utc_wire(row.occurred_at),
        "event_type": row.event_type,
        "event_version": row.event_version,
        "schema_version": row.schema_version,
        "producer": row.producer,
        "world_id": row.world_id,
        "session_id": row.session_id,
        "turn_id": row.turn_id,
        "command_id": row.command_id,
        "run_id": row.run_id,
        "commit_id": row.commit_id,
        "world_revision": row.world_revision,
        "action_index": row.action_index,
        "action_count": row.action_count,
        "intent_id": row.intent_id,
        "state_hash_before": row.state_hash_before,
        "state_hash_after": row.state_hash_after,
        "final_snapshot_revision": row.final_snapshot_revision,
        "final_world_event_sequence": row.final_world_event_sequence,
        "final_snapshot_state_hash": row.final_snapshot_state_hash,
        "payload_sha256": row.payload_sha256,
        "integrity_sha256": row.integrity_sha256,
    }
    if (
        any(event[field] != value for field, value in mirrors.items())
        or occurred_at != row.occurred_at
        or row.tenant_id != context.actor.tenant_id
        or row.tenant_id != snapshot_actor.tenant_id
        or row.actor_id != context.actor.actor_id
        or row.actor_id != snapshot_actor.actor_id
        or row.content_hash != snapshot_content.content_hash
        or event["final_snapshot_revision"] > snapshot.revision
        or event["final_world_event_sequence"] > snapshot.last_event_sequence
    ):
        raise ValueError("presentation row columns differ from the retained event bytes")
    return event


def _validate_page_events(
    events: list[dict[str, Any]],
    *,
    previous_event: dict[str, Any] | None,
    commit_groups: Mapping[str, list[dict[str, Any]]],
    after_sequence: int,
    high_watermark: int,
    initial_world_revision: int,
    initial_snapshot_state_hash: str,
    snapshot: WorldSnapshot,
    tenant_id: str,
) -> None:
    if not events:
        raise ValueError("a non-terminal presentation read returned no events")
    expected_sequence = after_sequence + 1
    event_ids: set[str] = set()
    for event in events:
        if (
            event["sequence"] != expected_sequence
            or event["event_id"] in event_ids
            or event["tenant_id"] != tenant_id
            or event["world_id"] != snapshot.world_id
            or event["stream_id"] != f"world-presentation:{snapshot.world_id}"
        ):
            raise ValueError("presentation events are not a contiguous authorized page")
        event_ids.add(str(event["event_id"]))
        expected_sequence += 1

    first = events[0]
    prior_hash = initial_snapshot_state_hash
    prior_revision = initial_world_revision
    if previous_event is not None:
        if previous_event["sequence"] != after_sequence:
            raise ValueError("presentation cursor differs from its durable event")
        prior_hash = str(previous_event["state_hash_after"])
        prior_revision = int(previous_event["world_revision"])
        if previous_event["commit_id"] == first["commit_id"]:
            if first["action_index"] != previous_event["action_index"] + 1:
                raise ValueError("presentation cursor cuts a corrupt committed action set")
        elif (
            previous_event["action_index"] != previous_event["action_count"] - 1
            or first["action_index"] != 0
            or int(first["world_revision"]) != prior_revision + 1
            or int(first["final_world_event_sequence"])
            <= int(previous_event["final_world_event_sequence"])
        ):
            raise ValueError("presentation cursor crosses an invalid commit boundary")
    elif first["action_index"] != 0 or int(first["world_revision"]) != prior_revision + 1:
        raise ValueError("presentation stream does not begin at its initial World head")
    if first["state_hash_before"] != prior_hash:
        raise ValueError("presentation page does not chain from its durable cursor")

    for commit_id in dict.fromkeys(str(event["commit_id"]) for event in events):
        group = commit_groups.get(commit_id)
        if not group:
            raise ValueError("presentation commit has no durable action set")
        start = group[0]
        action_count = int(start["action_count"])
        if action_count < 1 or len(group) != action_count:
            raise ValueError("presentation commit action set is incomplete")
        binding_fields = (
            "tenant_id",
            "session_id",
            "turn_id",
            "command_id",
            "run_id",
            "world_id",
            "commit_id",
            "world_revision",
            "action_count",
            "final_snapshot_revision",
            "final_world_event_sequence",
            "final_snapshot_state_hash",
        )
        for action_index, event in enumerate(group):
            if event["action_index"] != action_index or any(
                event[field] != start[field] for field in binding_fields
            ):
                raise ValueError("presentation commit identity or action indexes drifted")
            if action_index and (
                group[action_index - 1]["state_hash_after"] != event["state_hash_before"]
            ):
                raise ValueError("presentation reducer hash chain is broken")
        revision = int(start["world_revision"])
        if (
            start["final_snapshot_revision"] != revision
            or group[-1]["state_hash_after"] != start["final_snapshot_state_hash"]
        ):
            raise ValueError("presentation commit does not close its World revision")

    for left, right in zip(events, events[1:], strict=False):
        if left["state_hash_after"] != right["state_hash_before"]:
            raise ValueError("presentation reducer hash chain is broken")
        if left["commit_id"] == right["commit_id"]:
            if right["action_index"] != left["action_index"] + 1:
                raise ValueError("presentation action index sequence is broken")
        elif (
            left["action_index"] != left["action_count"] - 1
            or right["action_index"] != 0
            or int(right["world_revision"]) != int(left["world_revision"]) + 1
            or int(right["final_world_event_sequence"])
            <= int(left["final_world_event_sequence"])
        ):
            raise ValueError("presentation commit sequence is broken")

    last = events[-1]
    if int(last["sequence"]) == high_watermark and (
        last["action_index"] != last["action_count"] - 1
        or
        last["final_snapshot_revision"] != snapshot.revision
        or last["final_world_event_sequence"] != snapshot.last_event_sequence
        or last["final_snapshot_state_hash"] != snapshot.state_hash
    ):
        raise ValueError("presentation high-watermark differs from the current World snapshot")


def _empty_page(
    snapshot: WorldSnapshot, after_sequence: int, high_watermark: int
) -> Mapping[str, Any]:
    if after_sequence != high_watermark:
        raise ValueError("empty presentation page cursor differs from its high-watermark")
    return {
        "request_context": request_context_data(snapshot.request_context),
        "world_id": snapshot.world_id,
        "snapshot_revision": snapshot.revision,
        "snapshot_last_event_sequence": snapshot.last_event_sequence,
        "snapshot_state_hash": snapshot.state_hash,
        "presentation_high_watermark": high_watermark,
        "from_sequence": after_sequence,
        "to_sequence": after_sequence,
        "has_more": False,
        "next_after_sequence": after_sequence,
        "events": [],
    }


def _event_gap(message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        code="EVENT_SEQUENCE_GAP",
        category=ErrorCategory.CONCURRENCY,
        retryable=True,
        user_message_key="event.resync_required",
        stage="READ",
        message=message,
    )


def _payload_integrity_array(payload: Mapping[str, Any]) -> list[Any]:
    if set(payload) != _PAYLOAD_KEYS:
        raise ValueError("presentation payload keys differ from the HARVEST wire")
    position = _mapping(payload.get("position"), "payload.position")
    if set(position) != {"x", "y"}:
        raise ValueError("presentation position keys are malformed")
    actor = payload.get("actor_entity_id")
    plot = payload.get("plot_id")
    x = position.get("x")
    y = position.get("y")
    crop_type = payload.get("crop_type")
    growth_stage = payload.get("growth_stage")
    ready = payload.get("ready_to_harvest")
    if (
        not isinstance(actor, str)
        or _IDENTIFIER.fullmatch(actor) is None
        or not isinstance(plot, str)
        or _IDENTIFIER.fullmatch(plot) is None
        or not isinstance(x, int)
        or isinstance(x, bool)
        or not -100_000 <= x <= 100_000
        or not isinstance(y, int)
        or isinstance(y, bool)
        or not -100_000 <= y <= 100_000
        or not isinstance(crop_type, str)
        or _CROP_TYPE.fullmatch(crop_type) is None
        or not isinstance(growth_stage, int)
        or isinstance(growth_stage, bool)
        or not 0 <= growth_stage <= 100
        or ready is not True
    ):
        raise ValueError("presentation HARVEST payload is malformed")
    return [actor, plot, x, y, crop_type, growth_stage, ready]


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} is not an object")
    return dict(value)


def _utc_wire(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("presentation timestamp must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None
