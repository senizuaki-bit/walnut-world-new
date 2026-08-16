from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_runtime import (  # noqa: E402
    PEDAGOGY_POLICY_VERSION,
    EvidenceStage,
    LearnerCompetencySummary,
    PedagogyEvidence,
    PedagogyEvidenceOutcome,
    PedagogyInput,
    PedagogyPolicy,
    PedagogyPolicyError,
    TeachingPhase,
    phase_allowed_for_role,
)

NOW = datetime(2026, 8, 9, 9, 30, tzinfo=UTC)
DAY = timedelta(days=1)


def _evidence(
    evidence_id: str,
    outcome: PedagogyEvidenceOutcome,
    *,
    concept: str | None = "loop_boundary",
    occurred_at: datetime = NOW,
) -> PedagogyEvidence:
    return PedagogyEvidence(
        evidence_id=evidence_id,
        outcome=outcome,
        occurred_at=occurred_at,
        concept=concept,
    )


def _competency(
    concept: str,
    evidence_id: str,
    *,
    stage: EvidenceStage = EvidenceStage.DEMONSTRATED,
    assistance_level: int = 0,
    next_review_at: datetime = NOW + DAY,
) -> LearnerCompetencySummary:
    return LearnerCompetencySummary(
        concept=concept,
        evidence_stage=stage,
        assistance_level=assistance_level,
        next_review_at=next_review_at,
        evidence_ids=(evidence_id,),
    )


def _input(
    *,
    role="world_agent",
    event_type="task_started",
    failure_count: int = 0,
    hint_requested: bool = False,
    teaching_spec_version: str = "teaching_watering_v1",
    task_concepts: tuple[str, ...] = ("loop_boundary",),
    max_hint_level: int = 4,
    learner_revision: int = 0,
    learner_competencies: tuple[LearnerCompetencySummary, ...] = (),
    current_validated_evidence: tuple[PedagogyEvidence, ...] = (),
    event_time: datetime = NOW,
) -> PedagogyInput:
    learner_ids = tuple(
        evidence_id
        for competency in learner_competencies
        for evidence_id in competency.evidence_ids
    )
    return PedagogyInput(
        role=role,
        event_type=event_type,
        failure_count=failure_count,
        hint_requested=hint_requested,
        teaching_spec_version=teaching_spec_version,
        task_concepts=task_concepts,
        max_hint_level=max_hint_level,
        learner_revision=learner_revision,
        learner_competencies=learner_competencies,
        learner_evidence_ids=learner_ids,
        current_validated_evidence=current_validated_evidence,
        event_time=event_time,
    )


class PedagogyPolicyPhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PedagogyPolicy()

    def test_review_for_new_learner_is_low_disclosure_and_evidence_free(self) -> None:
        directive = self.policy.decide(_input())

        self.assertIsNotNone(directive)
        assert directive is not None
        self.assertEqual(directive.phase, TeachingPhase.REVIEW)
        self.assertEqual(directive.target_concept, "loop_boundary")
        self.assertEqual(directive.hint_level, 0)
        self.assertEqual(directive.allowed_response_types, ("message",))
        self.assertEqual(directive.required_evidence_ids, ())
        self.assertIn("LEARNER_REVISION_ZERO", directive.reason_codes)

    def test_heuristic_when_projected_competency_is_ready(self) -> None:
        competency = _competency("loop_boundary", "evidence_profile_ready")

        directive = self.policy.decide(
            _input(learner_revision=7, learner_competencies=(competency,))
        )

        assert directive is not None
        self.assertEqual(directive.phase, TeachingPhase.HEURISTIC)
        self.assertEqual(directive.required_evidence_ids, ("evidence_profile_ready",))
        self.assertIn("LEARNER_READY_FOR_EXPLORATION", directive.reason_codes)

    def test_rectification_selects_task_order_and_only_relevant_failures(self) -> None:
        boundary_failure = _evidence(
            "evidence_boundary_fail",
            PedagogyEvidenceOutcome.FAILED,
            concept="loop_boundary",
        )
        loop_failure = _evidence(
            "evidence_loop_failure",
            PedagogyEvidenceOutcome.FAILED,
            concept="for_loop",
        )

        directive = self.policy.decide(
            _input(
                role="teaching_agent",
                event_type="compile_failed",
                failure_count=2,
                task_concepts=("for_loop", "loop_boundary"),
                current_validated_evidence=(boundary_failure, loop_failure),
            )
        )

        assert directive is not None
        self.assertEqual(directive.phase, TeachingPhase.RECTIFICATION)
        self.assertEqual(directive.target_concept, "for_loop")
        self.assertEqual(directive.required_evidence_ids, ("evidence_loop_failure",))
        self.assertEqual(directive.hint_level, 1)

    def test_summarization_requires_and_binds_success_evidence(self) -> None:
        success = _evidence(
            "evidence_world_success",
            PedagogyEvidenceOutcome.SUCCESS,
        )

        directive = self.policy.decide(
            _input(
                role="book_agent",
                event_type="task_completed",
                current_validated_evidence=(success,),
            )
        )

        assert directive is not None
        self.assertEqual(directive.phase, TeachingPhase.SUMMARIZATION)
        self.assertEqual(directive.hint_level, 0)
        self.assertEqual(directive.allowed_response_types, ("growth_summary",))
        self.assertEqual(directive.required_evidence_ids, ("evidence_world_success",))

    def test_all_directives_disable_patch_and_full_solution(self) -> None:
        ready = _competency("loop_boundary", "evidence_profile_ready")
        failure = _evidence(
            "evidence_current_fail",
            PedagogyEvidenceOutcome.FAILED,
        )
        success = _evidence(
            "evidence_world_success",
            PedagogyEvidenceOutcome.SUCCESS,
        )
        inputs = (
            _input(),
            _input(learner_revision=1, learner_competencies=(ready,)),
            _input(
                role="teaching_agent",
                event_type="compile_failed",
                failure_count=1,
                current_validated_evidence=(failure,),
            ),
            _input(
                role="book_agent",
                event_type="task_completed",
                current_validated_evidence=(success,),
            ),
        )

        directives = tuple(self.policy.decide(item) for item in inputs)

        self.assertEqual(
            tuple(item.phase for item in directives if item is not None),
            (
                TeachingPhase.REVIEW,
                TeachingPhase.HEURISTIC,
                TeachingPhase.RECTIFICATION,
                TeachingPhase.SUMMARIZATION,
            ),
        )
        for directive in directives:
            assert directive is not None
            self.assertFalse(directive.patch_eligible)
            self.assertFalse(directive.full_solution_eligible)
            self.assertIn("PATCH_DISABLED_RUNTIME_STAGE", directive.reason_codes)
            self.assertIn("FULL_SOLUTION_DISABLED", directive.reason_codes)

    def test_allowed_response_types_are_role_specific(self) -> None:
        ready = _competency("loop_boundary", "evidence_profile_ready")
        failure = _evidence(
            "evidence_current_fail",
            PedagogyEvidenceOutcome.FAILED,
        )
        success = _evidence(
            "evidence_world_success",
            PedagogyEvidenceOutcome.SUCCESS,
        )
        directives = (
            self.policy.decide(_input()),
            self.policy.decide(
                _input(
                    role="teaching_agent",
                    event_type="hint_requested",
                    hint_requested=True,
                    learner_revision=1,
                    learner_competencies=(ready,),
                )
            ),
            self.policy.decide(
                _input(
                    role="bug_agent",
                    event_type="run_failed",
                    failure_count=3,
                    current_validated_evidence=(failure,),
                )
            ),
            self.policy.decide(
                _input(
                    role="book_agent",
                    event_type="task_completed",
                    current_validated_evidence=(success,),
                )
            ),
        )

        self.assertEqual(
            tuple(item.allowed_response_types for item in directives if item is not None),
            (("message",), ("question", "hint"), ("question",), ("growth_summary",)),
        )


class PedagogyPolicyBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PedagogyPolicy()

    def test_hint_level_respects_task_and_phase_caps(self) -> None:
        failure = _evidence(
            "evidence_current_fail",
            PedagogyEvidenceOutcome.FAILED,
        )
        phase_capped = self.policy.decide(
            _input(failure_count=9, hint_requested=True, max_hint_level=4)
        )
        task_capped = self.policy.decide(
            _input(
                role="bug_agent",
                event_type="run_failed",
                failure_count=9,
                hint_requested=True,
                max_hint_level=1,
                current_validated_evidence=(failure,),
            )
        )
        hard_capped = self.policy.decide(
            _input(
                role="bug_agent",
                event_type="run_failed",
                failure_count=9,
                hint_requested=True,
                max_hint_level=4,
                current_validated_evidence=(failure,),
            )
        )

        assert phase_capped is not None
        assert task_capped is not None
        assert hard_capped is not None
        self.assertEqual(phase_capped.hint_level, 1)
        self.assertIn("HINT_CAPPED_BY_PHASE", phase_capped.reason_codes)
        self.assertEqual(task_capped.hint_level, 1)
        self.assertIn("HINT_CAPPED_BY_TASK", task_capped.reason_codes)
        self.assertEqual(hard_capped.hint_level, 3)
        self.assertIn("HINT_CAPPED_BY_PHASE", hard_capped.reason_codes)

    def test_role_phase_compatibility_matrix_is_exhaustive(self) -> None:
        expected = {
            "world_agent": {TeachingPhase.REVIEW, TeachingPhase.HEURISTIC},
            "teaching_agent": {
                TeachingPhase.REVIEW,
                TeachingPhase.HEURISTIC,
                TeachingPhase.RECTIFICATION,
            },
            "bug_agent": {TeachingPhase.RECTIFICATION},
            "book_agent": {TeachingPhase.SUMMARIZATION},
            "xiaohutao": set(),
        }

        for role, allowed in expected.items():
            for phase in TeachingPhase:
                with self.subTest(role=role, phase=phase):
                    self.assertEqual(phase_allowed_for_role(role, phase), phase in allowed)

    def test_incompatible_role_event_and_bug_threshold_are_rejected(self) -> None:
        failure = _evidence(
            "evidence_current_fail",
            PedagogyEvidenceOutcome.FAILED,
        )
        invalid = (
            _input(role="book_agent", event_type="task_started"),
            _input(
                role="bug_agent",
                event_type="run_failed",
                failure_count=2,
                current_validated_evidence=(failure,),
            ),
            _input(
                role="teaching_agent",
                event_type="run_failed",
                failure_count=3,
                current_validated_evidence=(failure,),
            ),
        )

        for policy_input in invalid:
            with self.subTest(policy_input=policy_input):
                with self.assertRaises(PedagogyPolicyError):
                    self.policy.decide(policy_input)

    def test_xiaohutao_has_no_teaching_directive(self) -> None:
        directive = self.policy.decide(_input(role="xiaohutao", event_type="run_skill_requested"))

        self.assertIsNone(directive)

    def test_failure_and_completion_phases_require_objective_evidence(self) -> None:
        invalid = (
            _input(
                role="teaching_agent",
                event_type="compile_failed",
                failure_count=1,
            ),
            _input(
                role="teaching_agent",
                event_type="hint_requested",
                failure_count=1,
                hint_requested=True,
            ),
            _input(role="book_agent", event_type="task_completed"),
        )

        for policy_input in invalid:
            with self.subTest(policy_input=policy_input):
                with self.assertRaises(PedagogyPolicyError):
                    self.policy.decide(policy_input)

    def test_future_or_out_of_scope_evidence_is_rejected_before_decision(self) -> None:
        with self.assertRaises(ValueError):
            _input(
                current_validated_evidence=(
                    _evidence(
                        "evidence_future_fact",
                        PedagogyEvidenceOutcome.FAILED,
                        occurred_at=NOW + DAY,
                    ),
                )
            )
        with self.assertRaises(ValueError):
            _input(
                current_validated_evidence=(
                    _evidence(
                        "evidence_wrong_scope",
                        PedagogyEvidenceOutcome.FAILED,
                        concept="recursion",
                    ),
                )
            )

    def test_directive_is_deterministic_canonical_and_immutable(self) -> None:
        evidence_a = _evidence(
            "evidence_aaaaaaaa",
            PedagogyEvidenceOutcome.FAILED,
        )
        evidence_z = _evidence(
            "evidence_zzzzzzzz",
            PedagogyEvidenceOutcome.FAILED,
        )
        first = self.policy.decide(
            _input(
                role="teaching_agent",
                event_type="compile_failed",
                failure_count=2,
                current_validated_evidence=(evidence_z, evidence_a),
            )
        )
        second = self.policy.decide(
            _input(
                role="teaching_agent",
                event_type="compile_failed",
                failure_count=2,
                current_validated_evidence=(evidence_a, evidence_z),
            )
        )

        self.assertEqual(first, second)
        assert first is not None
        self.assertEqual(
            first.required_evidence_ids,
            ("evidence_aaaaaaaa", "evidence_zzzzzzzz"),
        )
        self.assertEqual(first.pedagogy_policy_version, PEDAGOGY_POLICY_VERSION)
        self.assertEqual(first.learner_revision, 0)
        self.assertEqual(first.teaching_spec_version, "teaching_watering_v1")
        with self.assertRaises(FrozenInstanceError):
            setattr(first, "hint_level", 4)


class PedagogyPolicyLearnerEffectsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PedagogyPolicy()

    def test_revision_missing_stage_assistance_and_review_time_affect_phase(self) -> None:
        observed = _competency(
            "loop_boundary",
            "evidence_observed_stage",
            stage=EvidenceStage.OBSERVED,
        )
        assisted = _competency(
            "loop_boundary",
            "evidence_assisted_stage",
            assistance_level=7,
        )
        due = _competency(
            "loop_boundary",
            "evidence_due_review",
            stage=EvidenceStage.TRANSFERRED,
            next_review_at=NOW,
        )
        ready = _competency(
            "loop_boundary",
            "evidence_ready_stage",
            stage=EvidenceStage.RETAINED,
            next_review_at=NOW + DAY,
        )
        cases = (
            (_input(), TeachingPhase.REVIEW, "LEARNER_REVISION_ZERO"),
            (
                _input(
                    task_concepts=("loop_boundary", "for_loop"),
                    learner_revision=1,
                    learner_competencies=(ready,),
                ),
                TeachingPhase.REVIEW,
                "LEARNER_CONCEPT_UNOBSERVED",
            ),
            (
                _input(learner_revision=1, learner_competencies=(observed,)),
                TeachingPhase.REVIEW,
                "LEARNER_STAGE_OBSERVED",
            ),
            (
                _input(learner_revision=2, learner_competencies=(assisted,)),
                TeachingPhase.REVIEW,
                "LEARNER_HIGH_ASSISTANCE",
            ),
            (
                _input(learner_revision=3, learner_competencies=(due,)),
                TeachingPhase.REVIEW,
                "LEARNER_REVIEW_DUE",
            ),
            (
                _input(learner_revision=4, learner_competencies=(ready,)),
                TeachingPhase.HEURISTIC,
                "LEARNER_READY_FOR_EXPLORATION",
            ),
        )

        for policy_input, expected_phase, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                directive = self.policy.decide(policy_input)
                assert directive is not None
                self.assertEqual(directive.phase, expected_phase)
                self.assertIn(expected_reason, directive.reason_codes)

    def test_review_due_boundary_uses_only_explicit_event_time(self) -> None:
        competency = _competency(
            "loop_boundary",
            "evidence_review_clock",
            next_review_at=NOW,
        )

        before = self.policy.decide(
            _input(
                learner_revision=1,
                learner_competencies=(competency,),
                event_time=NOW - timedelta(microseconds=1),
            )
        )
        due = self.policy.decide(
            _input(
                learner_revision=1,
                learner_competencies=(competency,),
                event_time=NOW,
            )
        )

        assert before is not None
        assert due is not None
        self.assertEqual(before.phase, TeachingPhase.HEURISTIC)
        self.assertEqual(due.phase, TeachingPhase.REVIEW)


if __name__ == "__main__":
    unittest.main()
