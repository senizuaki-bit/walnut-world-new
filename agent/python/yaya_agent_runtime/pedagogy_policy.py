"""Deterministic teaching-phase and disclosure policy.

All facts required by the decision are explicit immutable inputs.  In
particular, this module never reads the wall clock and never reaches through a
port to fetch learner, task, or Evidence state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .domain import BUG_FAILURE_THRESHOLD, GameEventType, ResponseType, RoleId
from .learner_projection_policy import MAX_INDEPENDENT_ASSISTANCE, EvidenceStage

PEDAGOGY_POLICY_VERSION = "pedagogy_policy_v1"

_CONCEPT = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_EVIDENCE_ID = re.compile(r"^evidence_[A-Za-z0-9_-]{8,128}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")

_ROLES: frozenset[str] = frozenset(
    {"world_agent", "xiaohutao", "teaching_agent", "bug_agent", "book_agent"}
)
_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task_started",
        "compile_succeeded",
        "compile_failed",
        "run_skill_requested",
        "run_succeeded",
        "run_failed",
        "task_completed",
        "hint_requested",
        "skill_patch_requested",
        "skill_patch_confirmed",
    }
)
_RESPONSE_TYPES: frozenset[str] = frozenset(
    {"message", "question", "hint", "skill_patch", "growth_summary"}
)


class TeachingPhase(StrEnum):
    REVIEW = "REVIEW"
    HEURISTIC = "HEURISTIC"
    RECTIFICATION = "RECTIFICATION"
    SUMMARIZATION = "SUMMARIZATION"


class PedagogyEvidenceOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class PedagogyPolicyError(ValueError):
    """Raised when trusted facts cannot yield a safe TeachingDirective."""


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


def _freeze_ids(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    frozen = tuple(values)
    if len(frozen) > 64 or len(set(frozen)) != len(frozen):
        raise ValueError(f"{field_name} must contain at most 64 unique IDs")
    for value in frozen:
        _require_pattern(value, _EVIDENCE_ID, f"{field_name} item")
    return tuple(sorted(frozen))


@dataclass(frozen=True, slots=True)
class PedagogyEvidence:
    """A current-turn Evidence fact validated before policy evaluation."""

    evidence_id: str
    outcome: PedagogyEvidenceOutcome
    occurred_at: datetime
    concept: str | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.evidence_id, _EVIDENCE_ID, "evidence_id")
        try:
            outcome = PedagogyEvidenceOutcome(self.outcome)
        except (TypeError, ValueError) as error:
            raise ValueError("outcome is not supported") from error
        if self.concept is not None:
            _require_pattern(self.concept, _CONCEPT, "concept")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(
            self,
            "occurred_at",
            _require_datetime(self.occurred_at, "occurred_at"),
        )


@dataclass(frozen=True, slots=True)
class LearnerCompetencySummary:
    """Minimal task-scoped learner slice consumed by the pedagogy policy."""

    concept: str
    evidence_stage: EvidenceStage
    assistance_level: int
    next_review_at: datetime
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.concept, _CONCEPT, "concept")
        try:
            stage = EvidenceStage(self.evidence_stage)
        except (TypeError, ValueError) as error:
            raise ValueError("evidence_stage is not supported") from error
        _require_integer(self.assistance_level, "assistance_level", minimum=0, maximum=10)
        evidence_ids = _freeze_ids(self.evidence_ids, "evidence_ids")
        if not evidence_ids:
            raise ValueError("a learner competency summary requires Evidence")
        object.__setattr__(self, "evidence_stage", stage)
        object.__setattr__(
            self,
            "next_review_at",
            _require_datetime(self.next_review_at, "next_review_at"),
        )
        object.__setattr__(self, "evidence_ids", evidence_ids)


@dataclass(frozen=True, slots=True)
class PedagogyInput:
    """Complete, replay-safe input to :meth:`PedagogyPolicy.decide`."""

    role: RoleId
    event_type: GameEventType
    failure_count: int
    hint_requested: bool
    teaching_spec_version: str
    task_concepts: tuple[str, ...]
    max_hint_level: int
    learner_revision: int
    learner_competencies: tuple[LearnerCompetencySummary, ...]
    learner_evidence_ids: tuple[str, ...]
    current_validated_evidence: tuple[PedagogyEvidence, ...]
    event_time: datetime
    explicit_skill_patch_request: bool = False
    skill_patch_feature_enabled: bool = False
    skill_patch_capability_enabled: bool = False
    draft_authority_validated: bool = False

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError("role is not supported")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("event_type is not supported")
        _require_integer(self.failure_count, "failure_count", minimum=0, maximum=10_000)
        if not isinstance(self.hint_requested, bool):
            raise TypeError("hint_requested must be boolean")
        if self.event_type == "hint_requested" and not self.hint_requested:
            raise ValueError("hint_requested events require an explicit hint request")
        if self.event_type == "run_failed" and self.failure_count < 1:
            raise ValueError("run_failed requires a positive failure_count")
        for name in (
            "explicit_skill_patch_request",
            "skill_patch_feature_enabled",
            "skill_patch_capability_enabled",
            "draft_authority_validated",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        patch_gates = (
            self.explicit_skill_patch_request,
            self.skill_patch_feature_enabled,
            self.skill_patch_capability_enabled,
            self.draft_authority_validated,
        )
        if self.event_type != "skill_patch_requested" and any(patch_gates):
            raise ValueError("Skill Patch eligibility gates require skill_patch_requested")
        _require_pattern(self.teaching_spec_version, _VERSION, "teaching_spec_version")

        concepts = tuple(self.task_concepts)
        if len(concepts) > 64 or len(set(concepts)) != len(concepts):
            raise ValueError("task_concepts must contain at most 64 unique concepts")
        for concept in concepts:
            _require_pattern(concept, _CONCEPT, "task_concepts item")
        _require_integer(self.max_hint_level, "max_hint_level", minimum=0, maximum=4)
        _require_integer(self.learner_revision, "learner_revision", minimum=0)

        competencies = tuple(self.learner_competencies)
        if len(competencies) > 64 or any(
            not isinstance(item, LearnerCompetencySummary) for item in competencies
        ):
            raise TypeError("learner_competencies must contain at most 64 typed summaries")
        competency_concepts = tuple(item.concept for item in competencies)
        if len(set(competency_concepts)) != len(competency_concepts):
            raise ValueError("learner_competencies must contain unique concepts")
        if not set(competency_concepts).issubset(concepts):
            raise ValueError("learner competencies must be scoped to task_concepts")

        learner_evidence_ids = _freeze_ids(
            self.learner_evidence_ids,
            "learner_evidence_ids",
        )
        for competency in competencies:
            if not set(competency.evidence_ids).issubset(learner_evidence_ids):
                raise ValueError("competency Evidence must be present in learner_evidence_ids")
        if self.learner_revision == 0 and (competencies or learner_evidence_ids):
            raise ValueError("learner revision zero cannot carry projected learner facts")

        current_evidence = tuple(self.current_validated_evidence)
        if len(current_evidence) > 64 or any(
            not isinstance(item, PedagogyEvidence) for item in current_evidence
        ):
            raise TypeError("current_validated_evidence must contain at most 64 typed facts")
        current_ids = tuple(item.evidence_id for item in current_evidence)
        if len(set(current_ids)) != len(current_ids):
            raise ValueError("current_validated_evidence IDs must be unique")
        if any(
            item.concept is not None and item.concept not in concepts for item in current_evidence
        ):
            raise ValueError("current Evidence concepts must be scoped to task_concepts")

        event_time = _require_datetime(self.event_time, "event_time")
        if any(item.occurred_at > event_time for item in current_evidence):
            raise ValueError("current Evidence cannot occur after event_time")
        object.__setattr__(self, "task_concepts", concepts)
        object.__setattr__(self, "learner_competencies", competencies)
        object.__setattr__(self, "learner_evidence_ids", learner_evidence_ids)
        object.__setattr__(self, "current_validated_evidence", current_evidence)
        object.__setattr__(self, "event_time", event_time)


_ALLOWED_BY_ROLE: dict[str, tuple[ResponseType, ...]] = {
    "world_agent": ("message",),
    "teaching_agent": ("question", "hint"),
    "bug_agent": ("question",),
    "book_agent": ("growth_summary",),
    "xiaohutao": (),
}
_ALLOWED_OPTIONS_BY_PHASE: dict[TeachingPhase, frozenset[tuple[ResponseType, ...]]] = {
    TeachingPhase.REVIEW: frozenset({("message",), ("question", "hint")}),
    TeachingPhase.HEURISTIC: frozenset({("message",), ("question", "hint")}),
    TeachingPhase.RECTIFICATION: frozenset({("question", "hint"), ("question",), ("skill_patch",)}),
    TeachingPhase.SUMMARIZATION: frozenset({("growth_summary",)}),
}
_HINT_CAP_BY_PHASE: dict[TeachingPhase, int] = {
    TeachingPhase.REVIEW: 1,
    TeachingPhase.HEURISTIC: 2,
    TeachingPhase.RECTIFICATION: 3,
    TeachingPhase.SUMMARIZATION: 0,
}
_PHASES_BY_ROLE: dict[str, frozenset[TeachingPhase]] = {
    "world_agent": frozenset({TeachingPhase.REVIEW, TeachingPhase.HEURISTIC}),
    "teaching_agent": frozenset(
        {
            TeachingPhase.REVIEW,
            TeachingPhase.HEURISTIC,
            TeachingPhase.RECTIFICATION,
        }
    ),
    "bug_agent": frozenset({TeachingPhase.RECTIFICATION}),
    "book_agent": frozenset({TeachingPhase.SUMMARIZATION}),
    "xiaohutao": frozenset(),
}
_EVENTS_BY_ROLE: dict[str, frozenset[str]] = {
    "world_agent": frozenset({"task_started"}),
    "teaching_agent": frozenset(
        {"compile_failed", "run_failed", "hint_requested", "skill_patch_requested"}
    ),
    "bug_agent": frozenset({"run_failed", "hint_requested"}),
    "book_agent": frozenset({"task_completed"}),
    "xiaohutao": frozenset({"run_skill_requested"}),
}


@dataclass(frozen=True, slots=True)
class TeachingDirective:
    phase: TeachingPhase
    target_concept: str | None
    hint_level: int
    allowed_response_types: tuple[ResponseType, ...]
    patch_eligible: bool
    full_solution_eligible: bool
    required_evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    pedagogy_policy_version: str
    learner_revision: int
    teaching_spec_version: str

    def __post_init__(self) -> None:
        try:
            phase = TeachingPhase(self.phase)
        except (TypeError, ValueError) as error:
            raise ValueError("phase is not supported") from error
        if self.target_concept is not None:
            _require_pattern(self.target_concept, _CONCEPT, "target_concept")
        maximum_hint = 4 if self.patch_eligible else _HINT_CAP_BY_PHASE[phase]
        _require_integer(self.hint_level, "hint_level", minimum=0, maximum=maximum_hint)
        allowed = tuple(self.allowed_response_types)
        if any(item not in _RESPONSE_TYPES for item in allowed):
            raise ValueError("allowed_response_types contains an unsupported response type")
        if allowed not in _ALLOWED_OPTIONS_BY_PHASE[phase]:
            raise ValueError("allowed_response_types is incompatible with the frozen phase policy")
        if not isinstance(self.patch_eligible, bool):
            raise TypeError("patch_eligible must be boolean")
        if self.patch_eligible:
            if (
                phase is not TeachingPhase.RECTIFICATION
                or self.hint_level != 4
                or allowed != ("skill_patch",)
                or not self.required_evidence_ids
            ):
                raise ValueError(
                    "eligible Skill Patch requires RECTIFICATION/L4/exact Evidence and one response"
                )
        elif "skill_patch" in allowed:
            raise ValueError("ineligible directives cannot allow Skill Patch")
        if self.full_solution_eligible is not False:
            raise ValueError("full_solution_eligible is disabled in this runtime stage")
        required = _freeze_ids(self.required_evidence_ids, "required_evidence_ids")
        reasons = tuple(self.reason_codes)
        if not reasons or len(reasons) > 16 or len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must contain 1..16 unique codes")
        for reason in reasons:
            _require_pattern(reason, _REASON_CODE, "reason_codes item")
        _require_pattern(
            self.pedagogy_policy_version,
            _VERSION,
            "pedagogy_policy_version",
        )
        _require_integer(self.learner_revision, "learner_revision", minimum=0)
        _require_pattern(self.teaching_spec_version, _VERSION, "teaching_spec_version")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "allowed_response_types", allowed)
        object.__setattr__(self, "required_evidence_ids", required)
        object.__setattr__(self, "reason_codes", reasons)


def phase_allowed_for_role(role: RoleId, phase: TeachingPhase) -> bool:
    """Return the frozen first-version role/phase compatibility relation."""

    if role not in _ROLES:
        return False
    try:
        normalized_phase = TeachingPhase(phase)
    except (TypeError, ValueError):
        return False
    return normalized_phase in _PHASES_BY_ROLE[role]


def _base_hint_level(failure_count: int, hint_requested: bool) -> int:
    if failure_count <= 1:
        level = 0
    elif failure_count == 2:
        level = 1
    elif failure_count <= 4:
        level = 2
    else:
        level = 3
    if hint_requested:
        level += 1
    return level


class PedagogyPolicy:
    """Compute one immutable TeachingDirective from trusted facts."""

    def decide(self, policy_input: PedagogyInput) -> TeachingDirective | None:
        if not isinstance(policy_input, PedagogyInput):
            raise TypeError("policy_input must be PedagogyInput")
        if policy_input.role == "xiaohutao":
            # The execution-only apprentice returns a receipt and never enters
            # a teaching phase.
            return None
        if policy_input.event_type not in _EVENTS_BY_ROLE[policy_input.role]:
            raise PedagogyPolicyError("role is incompatible with event_type")
        if not policy_input.task_concepts:
            raise PedagogyPolicyError("a teaching directive requires a declared task concept")

        phase, target, required_ids, reasons = self._select_phase(policy_input)
        if not phase_allowed_for_role(policy_input.role, phase):
            raise PedagogyPolicyError("role is incompatible with selected teaching phase")

        patch_requested = policy_input.event_type == "skill_patch_requested"
        patch_eligible = patch_requested and all(
            (
                policy_input.explicit_skill_patch_request,
                policy_input.skill_patch_feature_enabled,
                policy_input.skill_patch_capability_enabled,
                policy_input.draft_authority_validated,
                policy_input.failure_count >= 4,
                policy_input.max_hint_level >= 4,
            )
        )
        raw_hint = _base_hint_level(
            policy_input.failure_count,
            policy_input.hint_requested,
        )
        phase_cap = _HINT_CAP_BY_PHASE[phase]
        hint_level = 4 if patch_eligible else min(raw_hint, policy_input.max_hint_level, phase_cap)
        if raw_hint > policy_input.max_hint_level:
            reasons.append("HINT_CAPPED_BY_TASK")
        if min(raw_hint, policy_input.max_hint_level) > phase_cap:
            reasons.append("HINT_CAPPED_BY_PHASE")
        if patch_eligible:
            reasons.extend(
                (
                    "EXPLICIT_SKILL_PATCH_REQUEST",
                    "PATCH_FEATURE_AND_CAPABILITY_ENABLED",
                    "DRAFT_BUILD_RUN_AUTHORITY_VALIDATED",
                )
            )
        elif patch_requested:
            reasons.append("PATCH_NOT_ELIGIBLE")
        else:
            reasons.append("PATCH_DISABLED_RUNTIME_STAGE")
        reasons.append("FULL_SOLUTION_DISABLED")

        return TeachingDirective(
            phase=phase,
            target_concept=target,
            hint_level=hint_level,
            allowed_response_types=(
                ("skill_patch",) if patch_eligible else _ALLOWED_BY_ROLE[policy_input.role]
            ),
            patch_eligible=patch_eligible,
            full_solution_eligible=False,
            required_evidence_ids=required_ids,
            reason_codes=tuple(reasons),
            pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
            learner_revision=policy_input.learner_revision,
            teaching_spec_version=policy_input.teaching_spec_version,
        )

    def _select_phase(
        self,
        policy_input: PedagogyInput,
    ) -> tuple[TeachingPhase, str, tuple[str, ...], list[str]]:
        failures = tuple(
            item
            for item in policy_input.current_validated_evidence
            if item.outcome is PedagogyEvidenceOutcome.FAILED
        )
        successes = tuple(
            item
            for item in policy_input.current_validated_evidence
            if item.outcome is PedagogyEvidenceOutcome.SUCCESS
        )

        if policy_input.event_type == "task_completed":
            if not successes:
                raise PedagogyPolicyError(
                    "SUMMARIZATION requires current validated success Evidence"
                )
            target = self._target_from_evidence(policy_input.task_concepts, successes)
            return (
                TeachingPhase.SUMMARIZATION,
                target,
                tuple(item.evidence_id for item in successes),
                ["TASK_COMPLETED_WITH_SUCCESS_EVIDENCE"],
            )

        if policy_input.event_type in {
            "compile_failed",
            "run_failed",
            "skill_patch_requested",
        }:
            return self._rectification(policy_input, failures)

        if policy_input.event_type == "hint_requested":
            if failures:
                return self._rectification(policy_input, failures)
            if policy_input.failure_count > 0:
                raise PedagogyPolicyError(
                    "failure-based hint requires current validated failure Evidence"
                )
            return self._review_or_heuristic(policy_input)

        if policy_input.event_type == "task_started":
            return self._review_or_heuristic(policy_input)

        raise PedagogyPolicyError("event_type does not produce a TeachingDirective")

    def _rectification(
        self,
        policy_input: PedagogyInput,
        failures: tuple[PedagogyEvidence, ...],
    ) -> tuple[TeachingPhase, str, tuple[str, ...], list[str]]:
        if not failures:
            raise PedagogyPolicyError("RECTIFICATION requires current validated failure Evidence")
        if policy_input.role == "bug_agent" and (
            policy_input.failure_count < BUG_FAILURE_THRESHOLD
        ):
            raise PedagogyPolicyError("bug_agent requires the same-failure threshold")
        if (
            policy_input.role == "teaching_agent"
            and policy_input.event_type in {"run_failed", "hint_requested"}
            and policy_input.failure_count >= BUG_FAILURE_THRESHOLD
        ):
            raise PedagogyPolicyError("same-failure threshold requires bug_agent")
        target = self._target_from_evidence(policy_input.task_concepts, failures)
        relevant = tuple(
            item.evidence_id for item in failures if item.concept is None or item.concept == target
        )
        if not relevant:
            raise PedagogyPolicyError("selected concept has no validated failure Evidence")
        return (
            TeachingPhase.RECTIFICATION,
            target,
            relevant,
            ["VALIDATED_FAILURE_EVIDENCE"],
        )

    @staticmethod
    def _target_from_evidence(
        task_concepts: tuple[str, ...],
        evidence: tuple[PedagogyEvidence, ...],
    ) -> str:
        evidenced = {item.concept for item in evidence if item.concept is not None}
        for concept in task_concepts:
            if concept in evidenced:
                return concept
        return task_concepts[0]

    @staticmethod
    def _review_or_heuristic(
        policy_input: PedagogyInput,
    ) -> tuple[TeachingPhase, str, tuple[str, ...], list[str]]:
        by_concept = {item.concept: item for item in policy_input.learner_competencies}
        first = policy_input.task_concepts[0]
        if policy_input.learner_revision == 0:
            return TeachingPhase.REVIEW, first, (), ["LEARNER_REVISION_ZERO"]

        for concept in policy_input.task_concepts:
            if concept not in by_concept:
                return TeachingPhase.REVIEW, concept, (), ["LEARNER_CONCEPT_UNOBSERVED"]
        for concept in policy_input.task_concepts:
            competency = by_concept[concept]
            if competency.next_review_at <= policy_input.event_time:
                return (
                    TeachingPhase.REVIEW,
                    concept,
                    competency.evidence_ids,
                    ["LEARNER_REVIEW_DUE"],
                )
        for concept in policy_input.task_concepts:
            competency = by_concept[concept]
            if competency.evidence_stage is EvidenceStage.OBSERVED:
                return (
                    TeachingPhase.REVIEW,
                    concept,
                    competency.evidence_ids,
                    ["LEARNER_STAGE_OBSERVED"],
                )
        for concept in policy_input.task_concepts:
            competency = by_concept[concept]
            if competency.assistance_level > MAX_INDEPENDENT_ASSISTANCE:
                return (
                    TeachingPhase.REVIEW,
                    concept,
                    competency.evidence_ids,
                    ["LEARNER_HIGH_ASSISTANCE"],
                )

        competency = by_concept[first]
        return (
            TeachingPhase.HEURISTIC,
            first,
            competency.evidence_ids,
            ["LEARNER_READY_FOR_EXPLORATION"],
        )


__all__ = [
    "LearnerCompetencySummary",
    "PEDAGOGY_POLICY_VERSION",
    "PedagogyEvidence",
    "PedagogyEvidenceOutcome",
    "PedagogyInput",
    "PedagogyPolicy",
    "PedagogyPolicyError",
    "TeachingDirective",
    "TeachingPhase",
    "phase_allowed_for_role",
]
