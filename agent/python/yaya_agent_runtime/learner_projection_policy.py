"""Pure, deterministic learner competency projection policy.

The policy in this module consumes already-authorized, already-validated
learning evidence.  It does not read a clock, a database, an event stream, or
an LLM.  Adapters remain responsible for stream ordering, receipts, CAS and
durability; this module only computes one immutable competency projection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

LEARNER_PROJECTION_POLICY_VERSION = "learner_projection_v1"
REVIEW_POLICY_VERSION = "review_v1"
MAX_INDEPENDENT_ASSISTANCE = 2
MAX_COMPETENCY_EVIDENCE_IDS = 64

_CONCEPT = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_EVIDENCE_ID = re.compile(r"^evidence_[A-Za-z0-9_-]{8,128}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")


class EvidenceStage(StrEnum):
    """Closed, monotonic learner evidence stages."""

    OBSERVED = "OBSERVED"
    DEMONSTRATED = "DEMONSTRATED"
    RETAINED = "RETAINED"
    TRANSFERRED = "TRANSFERRED"


class ProjectionOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class TaskRelation(StrEnum):
    """How the evidence-producing task relates to the learned concept."""

    STANDARD = "STANDARD"
    REVIEW = "REVIEW"
    TRANSFER = "TRANSFER"


class LearnerProjectionPolicyError(ValueError):
    """Raised when trusted inputs cannot be interpreted by this policy version."""


def _require_integer(
    value: object,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return value


def _require_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_pattern(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} has an invalid format")
    return value


def _freeze_evidence_ids(
    values: tuple[str, ...],
    field_name: str,
    *,
    maximum: int = 64,
    sort_values: bool = True,
) -> tuple[str, ...]:
    frozen = tuple(values)
    if len(frozen) > maximum or len(set(frozen)) != len(frozen):
        raise ValueError(f"{field_name} must contain at most {maximum} unique IDs")
    for value in frozen:
        _require_pattern(value, _EVIDENCE_ID, f"{field_name} item")
    return tuple(sorted(frozen)) if sort_values else frozen


@dataclass(frozen=True, slots=True)
class ProjectionEvidence:
    """One validated fact consumed in learner-stream sequence order."""

    evidence_ids: tuple[str, ...]
    concept: str
    outcome: ProjectionOutcome
    task_relation: TaskRelation
    assistance_level: int
    occurred_at: datetime
    source_sequence: int
    used_full_solution: bool = False
    used_skill_patch: bool = False

    def __post_init__(self) -> None:
        evidence_ids = _freeze_evidence_ids(
            self.evidence_ids,
            "evidence_ids",
            maximum=16,
        )
        if not evidence_ids:
            raise ValueError("ProjectionEvidence requires at least one Evidence ID")
        _require_pattern(self.concept, _CONCEPT, "concept")
        try:
            outcome = ProjectionOutcome(self.outcome)
        except (TypeError, ValueError) as error:
            raise ValueError("outcome is not supported") from error
        try:
            relation = TaskRelation(self.task_relation)
        except (TypeError, ValueError) as error:
            raise ValueError("task_relation is not supported") from error
        _require_integer(self.assistance_level, "assistance_level", minimum=0, maximum=10)
        _require_integer(self.source_sequence, "source_sequence", minimum=1)
        if not isinstance(self.used_full_solution, bool):
            raise TypeError("used_full_solution must be boolean")
        if not isinstance(self.used_skill_patch, bool):
            raise TypeError("used_skill_patch must be boolean")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "task_relation", relation)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(
            self,
            "occurred_at",
            _require_datetime(self.occurred_at, "occurred_at"),
        )


@dataclass(frozen=True, slots=True)
class CompetencyProjection:
    """Task-scoped, replayable projection for one concept."""

    concept: str
    evidence_stage: EvidenceStage
    assistance_level: int
    last_observed_at: datetime
    next_review_at: datetime
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.concept, _CONCEPT, "concept")
        try:
            stage = EvidenceStage(self.evidence_stage)
        except (TypeError, ValueError) as error:
            raise ValueError("evidence_stage is not supported") from error
        _require_integer(self.assistance_level, "assistance_level", minimum=0, maximum=10)
        observed_at = _require_datetime(self.last_observed_at, "last_observed_at")
        review_at = _require_datetime(self.next_review_at, "next_review_at")
        if review_at <= observed_at:
            raise ValueError("next_review_at must be after last_observed_at")
        object.__setattr__(self, "evidence_stage", stage)
        object.__setattr__(self, "last_observed_at", observed_at)
        object.__setattr__(self, "next_review_at", review_at)
        object.__setattr__(
            self,
            "evidence_ids",
            _freeze_evidence_ids(
                self.evidence_ids,
                "evidence_ids",
                maximum=MAX_COMPETENCY_EVIDENCE_IDS,
                sort_values=False,
            ),
        )
        if not self.evidence_ids:
            raise ValueError("a competency projection requires Evidence")


@dataclass(frozen=True, slots=True)
class ProjectionInput:
    """Complete deterministic input for one policy evaluation."""

    learner_revision: int
    learner_projection_policy_version: str
    review_policy_version: str
    evidence: ProjectionEvidence
    current: CompetencyProjection | None = None

    def __post_init__(self) -> None:
        _require_integer(self.learner_revision, "learner_revision", minimum=0)
        _require_pattern(
            self.learner_projection_policy_version,
            _VERSION,
            "learner_projection_policy_version",
        )
        _require_pattern(self.review_policy_version, _VERSION, "review_policy_version")
        if not isinstance(self.evidence, ProjectionEvidence):
            raise TypeError("evidence must be ProjectionEvidence")
        if self.current is not None:
            if not isinstance(self.current, CompetencyProjection):
                raise TypeError("current must be CompetencyProjection or None")
            if self.learner_revision == 0:
                raise ValueError("learner revision zero cannot carry a competency projection")
            if self.current.concept != self.evidence.concept:
                raise ValueError("current projection and Evidence concept must match")


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    competency: CompetencyProjection
    applied: bool
    stage_changed: bool
    reason_codes: tuple[str, ...]
    learner_projection_policy_version: str
    review_policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.competency, CompetencyProjection):
            raise TypeError("competency must be CompetencyProjection")
        if not isinstance(self.applied, bool) or not isinstance(self.stage_changed, bool):
            raise TypeError("applied and stage_changed must be boolean")
        reasons = tuple(self.reason_codes)
        if not reasons or len(reasons) > 16 or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must contain 1..16 unique codes")
        for reason in reasons:
            _require_pattern(reason, _REASON_CODE, "reason_codes item")
        _require_pattern(
            self.learner_projection_policy_version,
            _VERSION,
            "learner_projection_policy_version",
        )
        _require_pattern(self.review_policy_version, _VERSION, "review_policy_version")
        if not self.applied and self.stage_changed:
            raise ValueError("an unapplied result cannot change stage")
        object.__setattr__(self, "reason_codes", reasons)


_REVIEW_INTERVALS: dict[str, dict[EvidenceStage, timedelta]] = {
    REVIEW_POLICY_VERSION: {
        EvidenceStage.OBSERVED: timedelta(days=1),
        EvidenceStage.DEMONSTRATED: timedelta(days=3),
        EvidenceStage.RETAINED: timedelta(days=7),
        EvidenceStage.TRANSFERRED: timedelta(days=14),
    }
}


class LearnerProjectionPolicy:
    """Versioned stage and review scheduler with no external dependencies."""

    def project(self, policy_input: ProjectionInput) -> ProjectionResult:
        if not isinstance(policy_input, ProjectionInput):
            raise TypeError("policy_input must be ProjectionInput")
        if policy_input.learner_projection_policy_version != LEARNER_PROJECTION_POLICY_VERSION:
            raise LearnerProjectionPolicyError("unsupported learner_projection_policy_version")
        intervals = _REVIEW_INTERVALS.get(policy_input.review_policy_version)
        if intervals is None:
            raise LearnerProjectionPolicyError("unsupported review_policy_version")

        current = policy_input.current
        evidence = policy_input.evidence
        existing_ids: set[str] = set(current.evidence_ids) if current is not None else set()
        incoming_ids = set(evidence.evidence_ids)
        if current is not None and incoming_ids.issubset(existing_ids):
            return ProjectionResult(
                competency=current,
                applied=False,
                stage_changed=False,
                reason_codes=("EVIDENCE_ALREADY_PROJECTED",),
                learner_projection_policy_version=LEARNER_PROJECTION_POLICY_VERSION,
                review_policy_version=policy_input.review_policy_version,
            )
        if existing_ids.intersection(incoming_ids):
            raise LearnerProjectionPolicyError(
                "ProjectionEvidence partially overlaps already projected Evidence"
            )
        if current is not None and evidence.occurred_at < current.last_observed_at:
            raise LearnerProjectionPolicyError(
                "Evidence occurred before the current projection observation"
            )

        if current is None:
            competency = CompetencyProjection(
                concept=evidence.concept,
                evidence_stage=EvidenceStage.OBSERVED,
                assistance_level=evidence.assistance_level,
                last_observed_at=evidence.occurred_at,
                next_review_at=evidence.occurred_at + intervals[EvidenceStage.OBSERVED],
                evidence_ids=evidence.evidence_ids,
            )
            reasons = ["FIRST_VALID_EVIDENCE", "STAGE_OBSERVED"]
            if evidence.outcome is ProjectionOutcome.SUCCESS:
                reasons.append("SINGLE_EVIDENCE_CANNOT_SKIP_STAGE")
            self._append_assistance_reasons(reasons, evidence)
            return ProjectionResult(
                competency=competency,
                applied=True,
                stage_changed=True,
                reason_codes=tuple(reasons),
                learner_projection_policy_version=LEARNER_PROJECTION_POLICY_VERSION,
                review_policy_version=policy_input.review_policy_version,
            )

        next_stage, reasons = self._next_stage(current, evidence)
        stage_changed = next_stage is not current.evidence_stage
        next_review_at = self._next_review_at(
            current=current,
            evidence=evidence,
            next_stage=next_stage,
            stage_changed=stage_changed,
            intervals=intervals,
        )
        retained_evidence_ids = tuple((*current.evidence_ids, *evidence.evidence_ids))[
            -MAX_COMPETENCY_EVIDENCE_IDS:
        ]
        competency = CompetencyProjection(
            concept=current.concept,
            evidence_stage=next_stage,
            assistance_level=evidence.assistance_level,
            last_observed_at=evidence.occurred_at,
            next_review_at=next_review_at,
            evidence_ids=retained_evidence_ids,
        )
        return ProjectionResult(
            competency=competency,
            applied=True,
            stage_changed=stage_changed,
            reason_codes=tuple(reasons),
            learner_projection_policy_version=LEARNER_PROJECTION_POLICY_VERSION,
            review_policy_version=policy_input.review_policy_version,
        )

    @staticmethod
    def _append_assistance_reasons(reasons: list[str], evidence: ProjectionEvidence) -> None:
        if evidence.assistance_level > MAX_INDEPENDENT_ASSISTANCE:
            reasons.append("ASSISTANCE_BLOCKED_PROMOTION")
        if evidence.used_full_solution:
            reasons.append("FULL_SOLUTION_BLOCKED_PROMOTION")
        if evidence.used_skill_patch:
            reasons.append("SKILL_PATCH_BLOCKED_PROMOTION")

    def _next_stage(
        self,
        current: CompetencyProjection,
        evidence: ProjectionEvidence,
    ) -> tuple[EvidenceStage, list[str]]:
        reasons: list[str] = []
        if evidence.outcome is not ProjectionOutcome.SUCCESS:
            reasons.append("NON_SUCCESS_CANNOT_PROMOTE")
            return current.evidence_stage, reasons

        self._append_assistance_reasons(reasons, evidence)
        independently_successful = (
            evidence.assistance_level <= MAX_INDEPENDENT_ASSISTANCE
            and not evidence.used_full_solution
            and not evidence.used_skill_patch
        )
        if not independently_successful:
            return current.evidence_stage, reasons

        stage = current.evidence_stage
        if stage is EvidenceStage.OBSERVED:
            if evidence.task_relation is TaskRelation.STANDARD:
                return EvidenceStage.DEMONSTRATED, ["STANDARD_SUCCESS_DEMONSTRATED"]
            reasons.append("STANDARD_SUCCESS_REQUIRED")
            return stage, reasons
        if stage is EvidenceStage.DEMONSTRATED:
            if evidence.task_relation is TaskRelation.REVIEW:
                if evidence.occurred_at >= current.next_review_at:
                    return EvidenceStage.RETAINED, ["DUE_REVIEW_SUCCESS_RETAINED"]
                reasons.append("REVIEW_NOT_DUE")
                return stage, reasons
            if evidence.task_relation is TaskRelation.TRANSFER:
                reasons.append("STAGE_SKIP_BLOCKED")
            else:
                reasons.append("DUE_REVIEW_SUCCESS_REQUIRED")
            return stage, reasons
        if stage is EvidenceStage.RETAINED:
            if evidence.task_relation is TaskRelation.TRANSFER:
                return EvidenceStage.TRANSFERRED, ["TRANSFER_SUCCESS_TRANSFERRED"]
            reasons.append("TRANSFER_SUCCESS_REQUIRED")
            return stage, reasons

        reasons.append("HIGHEST_STAGE_RETAINED")
        return stage, reasons

    @staticmethod
    def _next_review_at(
        *,
        current: CompetencyProjection,
        evidence: ProjectionEvidence,
        next_stage: EvidenceStage,
        stage_changed: bool,
        intervals: dict[EvidenceStage, timedelta],
    ) -> datetime:
        independent_success = (
            evidence.outcome is ProjectionOutcome.SUCCESS
            and evidence.assistance_level <= MAX_INDEPENDENT_ASSISTANCE
            and not evidence.used_full_solution
            and not evidence.used_skill_patch
        )
        if stage_changed or (
            independent_success
            and evidence.task_relation is TaskRelation.TRANSFER
            and next_stage is EvidenceStage.TRANSFERRED
        ):
            return evidence.occurred_at + intervals[next_stage]

        if independent_success:
            # An early or unrelated success must not postpone an already
            # scheduled review.
            if current.next_review_at > evidence.occurred_at:
                return current.next_review_at
            return evidence.occurred_at + intervals[next_stage]

        # Failure, partial success or strong assistance requests a near review,
        # but never pushes an earlier future review farther away.
        candidate = evidence.occurred_at + intervals[EvidenceStage.OBSERVED]
        if current.next_review_at > evidence.occurred_at:
            return min(current.next_review_at, candidate)
        return candidate


__all__ = [
    "CompetencyProjection",
    "EvidenceStage",
    "LEARNER_PROJECTION_POLICY_VERSION",
    "LearnerProjectionPolicy",
    "LearnerProjectionPolicyError",
    "MAX_INDEPENDENT_ASSISTANCE",
    "MAX_COMPETENCY_EVIDENCE_IDS",
    "ProjectionEvidence",
    "ProjectionInput",
    "ProjectionOutcome",
    "ProjectionResult",
    "REVIEW_POLICY_VERSION",
    "TaskRelation",
]
