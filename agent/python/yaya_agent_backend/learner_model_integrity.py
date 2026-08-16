"""Closed integrity checks for persisted learner model snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hmac import compare_digest
from typing import cast

from yaya_agent_contracts import LearnerModelSnapshot
from yaya_agent_runtime.learner_projection_policy import (
    CompetencyProjection,
    EvidenceStage,
)

from .codec import internal_record_sha256, plain


def validated_learner_competencies(
    snapshot: LearnerModelSnapshot,
) -> tuple[dict[str, CompetencyProjection], dict[str, object]]:
    """Decode the deliberately closed competency JSON and close its Evidence refs."""

    parsed: dict[str, CompetencyProjection] = {}
    normalized: dict[str, object] = {}
    for key, raw_value in snapshot.competencies.items():
        if not isinstance(raw_value, Mapping):
            raise ValueError("learner competency must be a JSON object")
        raw = cast(Mapping[str, object], raw_value)
        if set(raw) != {
            "concept",
            "evidence_stage",
            "assistance_level",
            "last_observed_at",
            "next_review_at",
            "evidence_ids",
        }:
            raise ValueError("learner competency fields are not closed")
        raw_ids = raw["evidence_ids"]
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes, bytearray)):
            raise ValueError("learner competency evidence_ids must be an array")
        raw_id_values = cast(Sequence[object], raw_ids)
        if any(not isinstance(item, str) for item in raw_id_values):
            raise ValueError("learner competency Evidence identities must be strings")
        ids = tuple(cast(str, item) for item in raw_id_values)
        concept = raw["concept"]
        evidence_stage = raw["evidence_stage"]
        assistance_level = raw["assistance_level"]
        if not isinstance(concept, str):
            raise ValueError("learner competency concept must be a string")
        if not isinstance(evidence_stage, str):
            raise ValueError("learner competency evidence_stage must be a string")
        if isinstance(assistance_level, bool) or not isinstance(assistance_level, int):
            raise ValueError("learner competency assistance_level must be an integer")
        for time_field in ("last_observed_at", "next_review_at"):
            if not isinstance(raw[time_field], str):
                raise ValueError("learner competency timestamps must be strings")
        competency = CompetencyProjection(
            concept=concept,
            evidence_stage=EvidenceStage(evidence_stage),
            assistance_level=assistance_level,
            last_observed_at=datetime.fromisoformat(
                cast(str, raw["last_observed_at"]).replace("Z", "+00:00")
            ),
            next_review_at=datetime.fromisoformat(
                cast(str, raw["next_review_at"]).replace("Z", "+00:00")
            ),
            evidence_ids=ids,
        )
        if key != competency.concept:
            raise ValueError("learner competency key and concept differ")
        canonical = cast(dict[str, object], plain(competency))
        if plain(raw) != canonical:
            raise ValueError("learner competency is not in canonical closed form")
        parsed[key] = competency
        normalized[key] = canonical

    snapshot_evidence_ids = {item.evidence_id for item in snapshot.evidence_refs}
    projected_ids = {
        evidence_id for competency in parsed.values() for evidence_id in competency.evidence_ids
    }
    if not projected_ids.issubset(snapshot_evidence_ids):
        raise ValueError("learner competency references absent snapshot Evidence")
    return parsed, normalized


def validate_persisted_learner_snapshot(
    snapshot: LearnerModelSnapshot,
    *,
    learner_id: str,
    revision: object,
    projected_through_sequence: object,
    model_version: object,
    snapshot_sha256: object,
    updated_at: object,
) -> tuple[dict[str, CompetencyProjection], dict[str, object]]:
    """Verify row columns, canonical snapshot hash, and closed competency contents."""

    if (
        snapshot.learner_id != learner_id
        or snapshot.revision != revision
        or snapshot.projected_through_sequence != projected_through_sequence
        or snapshot.model_version != model_version
    ):
        raise ValueError("learner model columns and canonical snapshot drifted")
    if snapshot.revision != snapshot.projected_through_sequence:
        raise ValueError("learner model revision and source checkpoint diverged")
    if not isinstance(updated_at, datetime) or snapshot.updated_at != updated_at:
        raise ValueError("learner model updated_at and canonical snapshot drifted")
    if not isinstance(snapshot_sha256, str):
        raise ValueError("learner model is missing its canonical snapshot hash")
    actual_sha256 = internal_record_sha256(snapshot)
    if not compare_digest(snapshot_sha256, actual_sha256):
        raise ValueError("learner model canonical snapshot hash drifted")
    return validated_learner_competencies(snapshot)


__all__ = [
    "validate_persisted_learner_snapshot",
    "validated_learner_competencies",
]
