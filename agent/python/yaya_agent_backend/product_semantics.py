"""Pure cross-field validators for Product AgentInteraction wire projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from yaya_agent_contracts import ActorRef, canonical_json_sha256

_MAX_SAFE_SEQUENCE = 9_007_199_254_740_991


class ProductProjectionSemanticError(ValueError):
    """Raised when a shape-valid Product projection is not identity-closed."""


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProductProjectionSemanticError(f"{field_name} is not an object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise ProductProjectionSemanticError(f"{field_name} has a non-string key")
    return cast(Mapping[str, object], source)


def _sequence(value: object, field_name: str, *, minimum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_SAFE_SEQUENCE
    ):
        raise ProductProjectionSemanticError(f"{field_name} is outside its range")
    return value


def _instant(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ProductProjectionSemanticError(f"{field_name} is not a timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProductProjectionSemanticError(f"{field_name} is not a timestamp") from error


def _stable_actor(actor: Mapping[str, object], expected: ActorRef) -> bool:
    return (
        actor.get("tenant_id") == expected.tenant_id
        and actor.get("actor_id") == expected.actor_id
        and actor.get("actor_type") == expected.actor_type.value
    )


def _hash_without(value: Mapping[str, object], field_name: str) -> str:
    return canonical_json_sha256({key: item for key, item in value.items() if key != field_name})


def validate_interaction_semantics(
    payload: Mapping[str, object],
    *,
    authenticated_actor: ActorRef,
    expected_session_id: str,
    expected_interaction_id: str | None = None,
) -> None:
    """Validate Product invariants that JSON Schema cannot express."""

    context = _mapping(payload.get("request_context"), "request_context")
    actor = _mapping(context.get("actor"), "request_context.actor")
    content = _mapping(context.get("content_ref"), "request_context.content_ref")
    source = _mapping(payload.get("projection_source"), "projection_source")
    feedback = _mapping(payload.get("feedback"), "feedback")
    feedback_event = _mapping(payload.get("feedback_event"), "feedback_event")
    links = _mapping(payload.get("links"), "links")

    interaction_id = payload.get("interaction_id")
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    sequence = _sequence(payload.get("sequence"), "sequence", minimum=1)
    revision = _sequence(
        payload.get("interaction_revision"),
        "interaction_revision",
        minimum=1,
    )
    feedback_sha256 = canonical_json_sha256(feedback)
    expected_self = (
        f"/product-experience/v1/sessions/{expected_session_id}/agent-interactions/{interaction_id}"
    )
    created_at = _instant(payload.get("created_at"), "created_at")

    if (
        not _stable_actor(actor, authenticated_actor)
        or session_id != expected_session_id
        or (expected_interaction_id is not None and interaction_id != expected_interaction_id)
        or links.get("self") != expected_self
        or links.get("session_workspace")
        != f"/product-experience/v1/sessions/{session_id}/workspace"
        or feedback.get("session_id") != session_id
        or feedback.get("turn_id") != turn_id
        or feedback_event.get("event_id") != source.get("feedback_event_id")
        or feedback_event.get("command_id") != feedback.get("command_id")
        or feedback_event.get("content_ref") != content
        or feedback_event.get("stream_id") != f"agent-session:{session_id}"
        or feedback_event.get("occurred_at") != feedback.get("completed_at")
        or feedback_event.get("feedback_sha256") != feedback_sha256
        or source.get("actor") != actor
        or source.get("content_ref") != content
        or source.get("interaction_id") != interaction_id
        or source.get("session_id") != session_id
        or source.get("turn_id") != turn_id
        or source.get("sequence") != sequence
        or source.get("command_id") != feedback.get("command_id")
        or source.get("feedback_sha256") != feedback_sha256
        or source.get("role") != payload.get("role")
        or source.get("response_type") != payload.get("response_type")
        or source.get("question") != payload.get("question")
        or source.get("hint_level") != payload.get("hint_level")
        or source.get("committed_at") != payload.get("created_at")
        or source.get("source_sha256") != _hash_without(source, "source_sha256")
        or _instant(feedback.get("completed_at"), "feedback.completed_at") > created_at
        or _instant(payload.get("updated_at"), "updated_at") < created_at
    ):
        raise ProductProjectionSemanticError("AgentInteraction identity or hash closure drifted")

    evidence = feedback.get("evidence_refs")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
        raise ProductProjectionSemanticError("feedback evidence_refs is invalid")
    evidence_ids: set[str] = set()
    for raw_reference in cast(Sequence[object], evidence):
        reference = _mapping(raw_reference, "feedback evidence reference")
        evidence_id = reference.get("evidence_id")
        if not isinstance(evidence_id, str) or evidence_id in evidence_ids:
            raise ProductProjectionSemanticError("feedback evidence identities are not unique")
        evidence_ids.add(evidence_id)

    skill_patch_raw = payload.get("skill_patch")
    if skill_patch_raw is None:
        if source.get("skill_patch_sha256") is not None or links.get("skill_draft") is not None:
            raise ProductProjectionSemanticError("absent SkillPatch retained patch authority")
        skill_patch: Mapping[str, object] | None = None
    else:
        skill_patch = _mapping(skill_patch_raw, "skill_patch")
        if (
            skill_patch.get("interaction_id") != interaction_id
            or skill_patch.get("session_id") != session_id
            or skill_patch.get("turn_id") != turn_id
            or skill_patch.get("patch_sha256") != source.get("skill_patch_sha256")
            or skill_patch.get("patch_sha256") != _hash_without(skill_patch, "patch_sha256")
            or links.get("skill_draft")
            != (
                f"/product-experience/v1/sessions/{session_id}/"
                f"skill-drafts/{skill_patch.get('draft_id')}"
            )
        ):
            raise ProductProjectionSemanticError("SkillPatch authority drifted")

    decision_raw = payload.get("patch_decision")
    if decision_raw is None:
        if revision != 1:
            raise ProductProjectionSemanticError("undecided interaction revision is not one")
        return
    decision = _mapping(decision_raw, "patch_decision")
    decision_context = _mapping(decision.get("request_context"), "patch_decision.request_context")
    decision_links = _mapping(decision.get("links"), "patch_decision.links")
    if skill_patch is None or (
        decision_context.get("actor") != actor
        or decision_context.get("content_ref") != content
        or decision.get("session_id") != session_id
        or decision.get("turn_id") != turn_id
        or decision.get("interaction_id") != interaction_id
        or decision.get("interaction_revision_before") != revision - 1
        or decision.get("interaction_revision_after") != revision
        or decision.get("patch_id") != skill_patch.get("patch_id")
        or decision.get("patch_sha256") != skill_patch.get("patch_sha256")
        or decision.get("draft_id") != skill_patch.get("draft_id")
        or decision.get("skill_id") != skill_patch.get("skill_id")
        or decision_links.get("interaction") != expected_self
        or decision_links.get("skill_draft") != links.get("skill_draft")
        or _instant(decision.get("decided_at"), "patch_decision.decided_at")
        != _instant(payload.get("updated_at"), "updated_at")
    ):
        raise ProductProjectionSemanticError("PatchDecision authority drifted")


def validate_page_semantics(
    payload: Mapping[str, object],
    *,
    authenticated_actor: ActorRef,
    expected_session_id: str,
    expected_after_sequence: int,
    expected_limit: int,
) -> None:
    """Validate stable-watermark, pagination, and per-item Product semantics."""

    page_context = _mapping(payload.get("request_context"), "page request_context")
    page_actor = _mapping(page_context.get("actor"), "page actor")
    page_content = _mapping(page_context.get("content_ref"), "page content_ref")
    interactions_raw = payload.get("interactions")
    if not isinstance(interactions_raw, list):
        raise ProductProjectionSemanticError("page interactions is not an array")
    high = _sequence(payload.get("high_watermark_sequence"), "high watermark", minimum=0)
    if (
        payload.get("session_id") != expected_session_id
        or payload.get("requested_after_sequence") != expected_after_sequence
        or payload.get("requested_limit") != expected_limit
        or len(cast(list[object], interactions_raw)) > expected_limit
        or not _stable_actor(page_actor, authenticated_actor)
    ):
        raise ProductProjectionSemanticError("page request identity or limit drifted")

    if not interactions_raw:
        if (
            payload.get("from_sequence") is not None
            or payload.get("to_sequence") is not None
            or payload.get("has_more") is not False
            or payload.get("next_after_sequence") != expected_after_sequence
            or high != expected_after_sequence
        ):
            raise ProductProjectionSemanticError("empty page advanced its cursor")
        return

    identifiers: set[str] = set()
    expected_sequence = expected_after_sequence + 1
    for raw_interaction in cast(list[object], interactions_raw):
        interaction = _mapping(raw_interaction, "page interaction")
        interaction_context = _mapping(
            interaction.get("request_context"),
            "page interaction request_context",
        )
        interaction_id = interaction.get("interaction_id")
        if (
            interaction_context.get("actor") != page_actor
            or interaction_context.get("content_ref") != page_content
            or interaction.get("sequence") != expected_sequence
            or not isinstance(interaction_id, str)
            or interaction_id in identifiers
        ):
            raise ProductProjectionSemanticError("page items are not identity-closed and gap-free")
        validate_interaction_semantics(
            interaction,
            authenticated_actor=authenticated_actor,
            expected_session_id=expected_session_id,
            expected_interaction_id=interaction_id,
        )
        identifiers.add(interaction_id)
        expected_sequence += 1

    first = expected_after_sequence + 1
    last = expected_sequence - 1
    if (
        payload.get("from_sequence") != first
        or payload.get("to_sequence") != last
        or payload.get("next_after_sequence") != last
        or payload.get("has_more") != (last < high)
        or high < last
    ):
        raise ProductProjectionSemanticError("page cursor or high-watermark semantics drifted")


__all__ = [
    "ProductProjectionSemanticError",
    "validate_interaction_semantics",
    "validate_page_semantics",
]
