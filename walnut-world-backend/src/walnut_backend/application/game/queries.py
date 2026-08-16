"""Authorized Game read use cases built from durable contract ports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from yaya_agent_contracts import (
    CommandRecord,
    ContentRef,
    EvidenceRef,
    Failure,
    OperationContext,
    Result,
    RuntimeEvent,
    Success,
)
from yaya_agent_contracts.ports import CommandStorePort, EventStorePort

from walnut_backend.adapters.postgres.models import (
    command_record_data,
    domain_event_data,
    request_context_data,
    world_snapshot_data,
)
from walnut_backend.adapters.postgres.run_evidence import PostgresRunEvidenceStore
from walnut_backend.adapters.postgres.world import PostgresWorld, world_commit_identifier
from walnut_backend.adapters.postgres.world_presentation import PostgresWorldPresentation


def public_command_record_data(record: CommandRecord) -> dict[str, Any]:
    """Project durable Command bytes to the canonical public timestamp spelling."""

    value = command_record_data(record)
    value["evidence_refs"] = [
        _public_evidence_ref_data(reference) for reference in record.evidence_refs
    ]
    return value


def _public_evidence_ref_data(reference: EvidenceRef) -> dict[str, Any]:
    if reference.created_at.tzinfo is None:
        raise ValueError("EvidenceRef.created_at must include a timezone")
    value: dict[str, Any] = {
        "evidence_id": reference.evidence_id,
        "evidence_type": reference.evidence_type.value,
        "created_at": (
            reference.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        ),
    }
    if reference.sha256 is not None:
        value["sha256"] = reference.sha256
    if reference.uri is not None:
        value["uri"] = reference.uri
    return value


class GameQueries:
    """Read models for public Game operationIds; responses stay schema-neutral here."""

    def __init__(
        self,
        command_store: CommandStorePort,
        world: PostgresWorld,
        event_store: EventStorePort,
        world_presentation: PostgresWorldPresentation,
        run_evidence: PostgresRunEvidenceStore,
        *,
        realtime_wss_enabled: bool = False,
        client_event_batch_enabled: bool = False,
        public_realtime_url: str = "wss://localhost/v1/realtime",
    ) -> None:
        self._command_store = command_store
        self._world = world
        self._event_store = event_store
        self._world_presentation = world_presentation
        self._run_evidence = run_evidence
        self._realtime_wss_enabled = realtime_wss_enabled
        self._client_event_batch_enabled = client_event_batch_enabled
        self._public_realtime_url = public_realtime_url

    async def get_command(
        self, command_id: str, context: OperationContext
    ) -> Result[Mapping[str, Any]]:
        result = await self._command_store.get(command_id, context)
        if isinstance(result, Failure):
            return result
        return Success(public_command_record_data(result.value))

    async def get_world_snapshot(
        self, world_id: str, context: OperationContext
    ) -> Result[Mapping[str, Any]]:
        result = await self._world.get_actor_snapshot(world_id, context)
        if isinstance(result, Failure):
            return result
        return Success(world_snapshot_data(result.value))

    async def get_run(self, run_id: str, context: OperationContext) -> Result[Mapping[str, Any]]:
        return await self._run_evidence.get_run(run_id, context)

    async def get_evidence(
        self, evidence_id: str, context: OperationContext
    ) -> Result[Mapping[str, Any]]:
        return await self._run_evidence.get_evidence(evidence_id, context)

    async def get_bootstrap(self, context: OperationContext) -> Result[Mapping[str, Any]]:
        result = await self._world.get_latest_snapshot(context)
        if isinstance(result, Failure):
            return result
        world = result.value
        return Success(
            {
                "request_context": request_context_data(context),
                "api_version": "1.0.0",
                "server_time": datetime.now(UTC).isoformat(),
                "actor": {
                    "tenant_id": context.actor.tenant_id,
                    "actor_id": context.actor.actor_id,
                    "actor_type": context.actor.actor_type.value,
                    "roles": list(context.actor.roles),
                },
                "content": {
                    "unit_id": context.content_ref.unit_id,
                    "version": context.content_ref.version,
                    "content_hash": context.content_ref.content_hash,
                },
                "capabilities": {
                    "skill_builds": True,
                    "agent_sessions": True,
                    "world_event_stream": self._realtime_wss_enabled,
                    "client_event_batch": self._client_event_batch_enabled,
                    "evidence_query": True,
                },
                "limits": {
                    "max_source_files": 32,
                    "max_source_bytes": 1_048_576,
                    "max_client_events_per_batch": 500,
                    "max_agent_turn_chars": 4000,
                },
                "world": {
                    "world_id": world.world_id,
                    "revision": world.revision,
                    "stream_id": f"world:{world.world_id}",
                    "last_event_sequence": world.last_event_sequence,
                    "stream_protocol_version": "1.0.0",
                    "snapshot_url": f"/v1/worlds/{world.world_id}/snapshot",
                    "events_url": f"/v1/worlds/{world.world_id}/events",
                    "stream_url": self._public_realtime_url,
                },
            }
        )

    async def list_world_events(
        self, world_id: str, after_sequence: int, limit: int, context: OperationContext
    ) -> Result[Mapping[str, Any]]:
        snapshot_result = await self._world.get_actor_snapshot(world_id, context)
        if isinstance(snapshot_result, Failure):
            return snapshot_result
        snapshot = snapshot_result.value
        high_watermark = snapshot.last_event_sequence
        if after_sequence > high_watermark:
            return Failure(
                _event_gap("after_sequence is above the durable World high-watermark")
            )
        events_result = await self._event_store.read_stream(
            f"world:{world_id}", after_sequence, limit, context
        )
        if isinstance(events_result, Failure):
            return events_result
        events = events_result.value
        serialized = [domain_event_data(event) for event in events.items]
        expected_count = min(limit, high_watermark - after_sequence)
        try:
            _validate_contiguous_page(
                world_id,
                after_sequence,
                serialized,
                expected_count=expected_count,
                tenant_id=context.actor.tenant_id,
                snapshot_last_event_sequence=snapshot.last_event_sequence,
                snapshot_state_hash=snapshot.state_hash,
                content_unit_id=snapshot.request_context.content_ref.unit_id,
                content_version=snapshot.request_context.content_ref.version,
                content_hash=snapshot.request_context.content_ref.content_hash,
            )
        except (KeyError, TypeError, ValueError) as error:
            return Failure(_event_gap(str(error)))
        if serialized:
            from_sequence = serialized[0]["sequence"]
            to_sequence = serialized[-1]["sequence"]
        else:
            from_sequence = after_sequence
            to_sequence = after_sequence
        next_after = to_sequence
        has_more = to_sequence < high_watermark
        if (events.next_cursor is not None) != has_more:
            return Failure(_event_gap("EventStore cursor differs from World high-watermark"))
        return Success(
            {
                "request_context": request_context_data(snapshot.request_context),
                "world_id": world_id,
                "snapshot_revision": snapshot.revision,
                "from_sequence": from_sequence,
                "to_sequence": to_sequence,
                "has_more": has_more,
                "next_after_sequence": next_after,
                "events": serialized,
            }
        )

    async def list_world_presentation_events(
        self, world_id: str, after_sequence: int, limit: int, context: OperationContext
    ) -> Result[Mapping[str, Any]]:
        snapshot_result = await self._world.get_actor_snapshot(world_id, context)
        if isinstance(snapshot_result, Failure):
            return snapshot_result
        return await self._world_presentation.list_page(
            snapshot_result.value, after_sequence, limit, context
        )


def _validate_contiguous_page(
    world_id: str,
    after_sequence: int,
    events: list[dict[str, Any]],
    *,
    expected_count: int,
    tenant_id: str,
    snapshot_last_event_sequence: int,
    snapshot_state_hash: str,
    content_unit_id: str,
    content_version: str,
    content_hash: str,
) -> None:
    if len(events) != expected_count:
        raise ValueError("stored World event page differs from the snapshot high-watermark")
    expected = after_sequence + 1
    event_ids: set[str] = set()
    for event in events:
        content = event.get("content_ref")
        if (
            event["event_id"] in event_ids
            or event["stream_id"] != f"world:{world_id}"
            or event["sequence"] != expected
            or not isinstance(content, Mapping)
            or content.get("unit_id") != content_unit_id
            or content.get("version") != content_version
            or content.get("content_hash") != content_hash
        ):
            raise ValueError("stored World events are not a contiguous canonical page")
        try:
            runtime_event = RuntimeEvent(
                event_id=event["event_id"],
                event_type=event["event_type"],
                event_version=event["event_version"],
                stream_id=event["stream_id"],
                sequence=event["sequence"],
                occurred_at=datetime.fromisoformat(
                    event["occurred_at"].replace("Z", "+00:00")
                ),
                producer=event["producer"],
                trace_id=event["trace_id"],
                command_id=event["command_id"],
                correlation_id=event["correlation_id"],
                causation_id=event["causation_id"],
                content_ref=ContentRef(**dict(content)),
                payload=event["payload"],
                schema_version=event["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("stored World event violates the runtime event contract") from error
        payload = runtime_event.payload
        committed_at = datetime.fromisoformat(
            str(payload.get("committed_at")).replace("Z", "+00:00")
        )
        if (
            runtime_event.event_type != "world.committed"
            or payload.get("world_id") != world_id
            or payload.get("world_revision") != runtime_event.sequence
            or committed_at != runtime_event.occurred_at
            or payload.get("commit_id")
            != world_commit_identifier(
                tenant_id,
                runtime_event.stream_id,
                str(payload.get("run_id")),
                int(payload.get("previous_world_revision", -1)),
            )
            or (
                runtime_event.sequence == snapshot_last_event_sequence
                and payload.get("state_hash") != snapshot_state_hash
            )
        ):
            raise ValueError("stored World event does not close its World revision")
        event_ids.add(event["event_id"])
        expected += 1


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
