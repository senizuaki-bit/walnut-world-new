"""The sole gateway for handler JSON responses governed by the release schemas."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from fastapi import Request
from starlette.responses import Response

from walnut_backend.api.dependencies import OperationContext, get_operation_context
from walnut_backend.api.errors import TransportError, attempt_headers, error_response
from walnut_backend.domain.canonical_json import canonical_payload as _canonical_payload

SemanticInvariant = Callable[[Mapping[str, Any], OperationContext, Mapping[str, str]], None]


def contract_response(
    *,
    request: Request,
    payload: Mapping[str, Any],
    schema_path: str,
    resource_identity: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    status_code: int = 200,
    use_canonical_json: bool = True,
) -> Response:
    """Validate the released response contract before serializing it.

    ``YAYA_CANONICAL_JSON_V1`` only governs schemas that explicitly declare it.
    A released schema may therefore opt into ordinary finite JSON numbers while
    retaining all schema, semantic-invariant, identity, and header checks here.
    """
    context = get_operation_context(request)
    outgoing_headers = {**attempt_headers(context), **(headers or {})}
    try:
        validate_headers(outgoing_headers, context)
        schema_errors = request.app.state.contract_release.validate(schema_path, dict(payload))
        if schema_errors:
            raise TransportError("INVARIANT_VIOLATION", "RESPONSE_SCHEMA", schema_errors[0])
        validate_semantic_invariants(schema_path, payload, context, outgoing_headers)
        validate_identity(payload, resource_identity)
        wire_payload = (
            canonical_payload(payload) if use_canonical_json else _standard_json_payload(payload)
        )
    except TransportError as error:
        return error_response(error, context, request.app.state.error_catalog)
    return Response(
        content=wire_payload,
        media_type="application/json",
        headers=outgoing_headers,
        status_code=status_code,
    )


def _standard_json_payload(payload: Mapping[str, Any]) -> bytes:
    """Encode RFC 8259 JSON deterministically while rejecting NaN and infinities."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def validate_headers(headers: Mapping[str, str], context: OperationContext) -> None:
    expected = attempt_headers(context)
    if any(headers.get(name) != value for name, value in expected.items()):
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_HEADER")


def validate_identity(payload: Mapping[str, Any], identity: Mapping[str, str] | None) -> None:
    if identity is None:
        return
    for field, expected in identity.items():
        value: object = payload
        for segment in field.split("."):
            if not isinstance(value, Mapping):
                raise TransportError("INVARIANT_VIOLATION", "RESPONSE_IDENTITY")
            value = value.get(segment)
        if value != expected:
            raise TransportError("INVARIANT_VIOLATION", "RESPONSE_IDENTITY")


def validate_semantic_invariants(
    schema_path: str,
    payload: Mapping[str, Any],
    context: OperationContext,
    headers: Mapping[str, str],
) -> None:
    """Explicit transport operation invariants executed after JSON Schema validation."""
    request_context = payload.get("request_context")
    if isinstance(request_context, Mapping):
        authenticated_request_context_invariant(request_context, context)
    for invariant in SEMANTIC_INVARIANT_REGISTRY.get(schema_path, ()):
        invariant(payload, context, headers)


def authenticated_actor_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    """An ActorRef representation must be the actor derived from the Bearer credential."""
    actor = context.actor
    expected = {
        "tenant_id": actor.tenant_id,
        "actor_id": actor.actor_id,
        "actor_type": actor.actor_type,
        "roles": list(actor.roles),
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")


def authenticated_request_context_invariant(
    request_context: Mapping[str, Any], context: OperationContext
) -> None:
    """Every response origin context must preserve the authenticated actor."""
    actor = request_context.get("actor")
    if not isinstance(actor, Mapping):
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")
    expected = {
        "tenant_id": context.actor.tenant_id,
        "actor_id": context.actor.actor_id,
        "actor_type": context.actor.actor_type,
        "roles": list(context.actor.roles),
    }
    if any(actor.get(field) != value for field, value in expected.items()):
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")


def command_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    result = payload.get("result")
    if isinstance(result, Mapping) and result.get("result_type") == "WORLD_COMMIT":
        _require(result.get("world_revision") == result.get("previous_revision", -1) + 1)
        _require(result.get("last_event_sequence") >= result.get("first_event_sequence", 1))


def evidence_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    integrity = _object(payload, "integrity")
    evidence_ref = _object(payload, "evidence_ref")
    digest = hashlib.sha256(canonical_payload(_object(payload, "payload"))).hexdigest()
    _require(integrity.get("payload_sha256") == digest)
    _require(headers.get("ETag") == f'"{digest}"')
    if "sha256" in evidence_ref:
        _require(evidence_ref.get("sha256") == digest)
    source = _object(payload, "source")
    if _object(payload, "payload").get("evidence_kind") == "WORLD_COMMIT":
        _require(
            source.get("source_id") == source.get("world_id") == _object(payload, "payload").get("world_id")
        )


def run_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    application = _object(payload, "world_application")
    receipt = application.get("receipt")
    if isinstance(receipt, Mapping):
        _require(receipt.get("world_revision") == receipt.get("previous_revision", -1) + 1)
        _require(receipt.get("last_event_sequence") >= receipt.get("first_event_sequence", 1))
    feedback = payload.get("agent_feedback")
    if isinstance(feedback, Mapping):
        _require(
            all(feedback.get(field) == payload.get(field) for field in ("session_id", "turn_id", "command_id", "run_id"))
        )
        _require(_evidence_ids(feedback.get("evidence_refs")) == _evidence_ids(payload.get("evidence_refs")))


def world_event_page_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    events = payload.get("events")
    if not isinstance(events, list):
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")
    world_id = payload.get("world_id")
    event_ids: set[object] = set()
    expected: int | None = None
    for event in events:
        if not isinstance(event, Mapping):
            raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")
        sequence, event_id = event.get("sequence"), event.get("event_id")
        if not isinstance(sequence, int):
            raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")
        _require(sequence == expected if expected is not None else True)
        _require(event_id not in event_ids and event.get("stream_id") == f"world:{world_id}")
        event_ids.add(event_id)
        expected = sequence + 1
    if events:
        _require(payload.get("from_sequence") == events[0].get("sequence"))
        _require(payload.get("to_sequence") == events[-1].get("sequence"))
        _require(payload.get("next_after_sequence") == events[-1].get("sequence"))


def skill_activation_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    _require(payload.get("registry_revision") == payload.get("previous_registry_revision", -1) + 1)


def student_bootstrap_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    request_context = _object(payload, "request_context")
    actor = _object(payload, "actor")
    content = _object(payload, "content")
    session = _object(payload, "session")
    create_request = _object(session, "create_request")
    activation = _object(payload, "activation")
    scope = _object(activation, "scope")
    world = _object(payload, "world")
    _require(request_context.get("actor") == actor)
    _require(request_context.get("content_ref") == content)
    _require(actor.get("actor_type") == "student")
    _require(create_request.get("learner_id") == actor.get("actor_id"))
    _require(create_request.get("world_id") == world.get("world_id"))
    _require(create_request.get("content") == content)
    _require(create_request.get("expected_world_revision") == world.get("revision"))
    _require(scope.get("world_id") == world.get("world_id"))
    _require(scope.get("agent_profile_id") == create_request.get("agent_profile_id"))
    active = activation.get("active")
    if isinstance(active, Mapping):
        _require(active.get("registry_revision") == activation.get("registry_revision"))
    _require(world.get("snapshot_url") == f"/v1/worlds/{world.get('world_id')}/snapshot")
    _require(world.get("events_url") == f"/v1/worlds/{world.get('world_id')}/events")


def product_draft_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    request_context = _object(payload, "request_context")
    _require(request_context.get("content_ref") == payload.get("content_ref"))
    projection = {field: payload[field] for field in ("session_id", "draft_id", "skill_id", "content_ref", "display_name", "source_bundle")}
    _require(payload.get("draft_sha256") == hashlib.sha256(canonical_payload(projection)).hexdigest())
    _require(_time_not_before(payload.get("updated_at"), payload.get("created_at")))
    _require(headers.get("ETag") == f'"draft:{payload.get("revision")}:{payload.get("draft_sha256")}"')
    _require(headers.get("X-Draft-Revision") == str(payload.get("revision")))


def patch_receipt_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    _require(payload.get("interaction_revision_after") == payload.get("interaction_revision_before", -1) + 1)
    if payload.get("decision") == "ACCEPT":
        _require(payload.get("draft_revision_after") == payload.get("draft_revision_before", -1) + 1)
    else:
        _require(
            payload.get("draft_revision_after") == payload.get("draft_revision_before")
            and payload.get("draft_sha256_after") == payload.get("draft_sha256_before")
        )


def workspace_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    session = _object(payload, "session")
    request_context = _object(payload, "request_context")
    _require(request_context.get("content_ref") == payload.get("content_ref") == session.get("content"))
    _require(_object(payload, "world_checkpoint").get("world_id") == session.get("world_id"))
    _require(_time_not_before(payload.get("updated_at"), payload.get("created_at")))
    _require(headers.get("ETag", "").startswith(f'"workspace:{payload.get("workspace_revision")}:'))


def product_content_invariant(
    payload: Mapping[str, Any], context: OperationContext, headers: Mapping[str, str]
) -> None:
    content_ref = _object(payload, "content_ref")
    _require(headers.get("ETag") == f'"{content_ref.get("content_hash")}"')


def _object(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")
    return nested


def _require(condition: object) -> None:
    if not condition:
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")


def _evidence_ids(value: object) -> set[object]:
    if not isinstance(value, list):
        raise TransportError("INVARIANT_VIOLATION", "RESPONSE_INVARIANT")
    return {item.get("evidence_id") for item in value if isinstance(item, Mapping)}


def _time_not_before(later: object, earlier: object) -> bool:
    if not isinstance(later, str) or not isinstance(earlier, str):
        return False
    try:
        return datetime.fromisoformat(later.replace("Z", "+00:00")) >= datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    except ValueError:
        return False


SEMANTIC_INVARIANT_REGISTRY: dict[str, tuple[SemanticInvariant, ...]] = {
    "contracts/schemas/common/actor-ref.schema.json": (authenticated_actor_invariant,),
    "contracts/schemas/game/command.schema.json": (command_invariant,),
    "contracts/schemas/game/evidence.schema.json": (evidence_invariant,),
    "contracts/schemas/game/run.schema.json": (run_invariant,),
    "contracts/schemas/game/world-event-page.schema.json": (world_event_page_invariant,),
    "contracts/schemas/game/skill-activation.schema.json": (skill_activation_invariant,),
    "contracts/schemas/game/student-bootstrap-v2.schema.json": (student_bootstrap_invariant,),
    "contracts/schemas/product-experience/content-unit.schema.json": (product_content_invariant,),
    "contracts/schemas/product-experience/skill-draft.schema.json": (product_draft_invariant,),
    "contracts/schemas/product-experience/patch-decision-receipt.schema.json": (patch_receipt_invariant,),
    "contracts/schemas/product-experience/session-workspace.schema.json": (workspace_invariant,),
}


def canonical_payload(payload: Mapping[str, Any]) -> bytes:
    """Implement the locked YAYA_CANONICAL_JSON_V1 value constraints and bytes."""
    try:
        return _canonical_payload(payload)
    except (TypeError, ValueError) as error:
        raise TransportError("INVARIANT_VIOLATION", "CANONICAL_RESOURCE") from error
