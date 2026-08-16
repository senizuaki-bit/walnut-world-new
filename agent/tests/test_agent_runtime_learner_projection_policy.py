from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_runtime import (  # noqa: E402
    LEARNER_PROJECTION_POLICY_VERSION,
    REVIEW_POLICY_VERSION,
    CompetencyProjection,
    EvidenceStage,
    LearnerProjectionPolicy,
    LearnerProjectionPolicyError,
    ProjectionEvidence,
    ProjectionInput,
    ProjectionOutcome,
    TaskRelation,
)

NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _evidence(
    *evidence_ids: str,
    outcome: ProjectionOutcome = ProjectionOutcome.SUCCESS,
    relation: TaskRelation = TaskRelation.STANDARD,
    assistance_level: int = 0,
    occurred_at: datetime = NOW,
    source_sequence: int = 1,
    used_full_solution: bool = False,
    used_skill_patch: bool = False,
) -> ProjectionEvidence:
    return ProjectionEvidence(
        evidence_ids=evidence_ids or ("evidence_default1",),
        concept="loop_boundary",
        outcome=outcome,
        task_relation=relation,
        assistance_level=assistance_level,
        occurred_at=occurred_at,
        source_sequence=source_sequence,
        used_full_solution=used_full_solution,
        used_skill_patch=used_skill_patch,
    )


def _current(
    *,
    stage: EvidenceStage = EvidenceStage.OBSERVED,
    assistance_level: int = 0,
    last_observed_at: datetime = NOW,
    next_review_at: datetime = NOW + DAY,
    evidence_ids: tuple[str, ...] = ("evidence_existing1",),
) -> CompetencyProjection:
    return CompetencyProjection(
        concept="loop_boundary",
        evidence_stage=stage,
        assistance_level=assistance_level,
        last_observed_at=last_observed_at,
        next_review_at=next_review_at,
        evidence_ids=evidence_ids,
    )


def _input(
    evidence: ProjectionEvidence,
    *,
    current: CompetencyProjection | None = None,
    learner_revision: int | None = None,
    projection_version: str = LEARNER_PROJECTION_POLICY_VERSION,
    review_version: str = REVIEW_POLICY_VERSION,
) -> ProjectionInput:
    return ProjectionInput(
        learner_revision=(1 if current is not None else 0)
        if learner_revision is None
        else learner_revision,
        learner_projection_policy_version=projection_version,
        review_policy_version=review_version,
        evidence=evidence,
        current=current,
    )


class LearnerProjectionStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = LearnerProjectionPolicy()

    def test_one_multi_reference_success_is_one_observation_only(self) -> None:
        evidence = _evidence(
            "evidence_actionlog1",
            "evidence_worldfact1",
            "evidence_testreport",
        )

        result = self.policy.project(_input(evidence))

        self.assertTrue(result.applied)
        self.assertTrue(result.stage_changed)
        self.assertEqual(result.competency.evidence_stage, EvidenceStage.OBSERVED)
        self.assertEqual(
            result.competency.evidence_ids,
            (
                "evidence_actionlog1",
                "evidence_testreport",
                "evidence_worldfact1",
            ),
        )
        self.assertIn("SINGLE_EVIDENCE_CANNOT_SKIP_STAGE", result.reason_codes)
        self.assertEqual(result.competency.next_review_at, NOW + DAY)

    def test_closed_stage_chain_advances_exactly_one_stage_per_observation(self) -> None:
        observed = self.policy.project(
            _input(_evidence("evidence_observation1", occurred_at=NOW))
        ).competency
        demonstrated_time = NOW + timedelta(hours=1)
        demonstrated = self.policy.project(
            _input(
                _evidence(
                    "evidence_demonstrate1",
                    occurred_at=demonstrated_time,
                    source_sequence=2,
                ),
                current=observed,
            )
        ).competency
        retained_time = demonstrated.next_review_at
        retained = self.policy.project(
            _input(
                _evidence(
                    "evidence_due_review1",
                    relation=TaskRelation.REVIEW,
                    occurred_at=retained_time,
                    source_sequence=3,
                ),
                current=demonstrated,
            )
        ).competency
        transferred_time = retained_time + timedelta(hours=1)
        transferred = self.policy.project(
            _input(
                _evidence(
                    "evidence_transfer01",
                    relation=TaskRelation.TRANSFER,
                    occurred_at=transferred_time,
                    source_sequence=4,
                ),
                current=retained,
            )
        ).competency

        self.assertEqual(
            (
                observed.evidence_stage,
                demonstrated.evidence_stage,
                retained.evidence_stage,
                transferred.evidence_stage,
            ),
            (
                EvidenceStage.OBSERVED,
                EvidenceStage.DEMONSTRATED,
                EvidenceStage.RETAINED,
                EvidenceStage.TRANSFERRED,
            ),
        )
        self.assertEqual(demonstrated.next_review_at, demonstrated_time + 3 * DAY)
        self.assertEqual(retained.next_review_at, retained_time + 7 * DAY)
        self.assertEqual(transferred.next_review_at, transferred_time + 14 * DAY)

    def test_transfer_evidence_cannot_skip_demonstrated_or_retained(self) -> None:
        current = _current(stage=EvidenceStage.DEMONSTRATED)
        evidence = _evidence(
            "evidence_early_xfer",
            relation=TaskRelation.TRANSFER,
            occurred_at=NOW + timedelta(hours=1),
            source_sequence=2,
        )

        result = self.policy.project(_input(evidence, current=current))

        self.assertEqual(result.competency.evidence_stage, EvidenceStage.DEMONSTRATED)
        self.assertFalse(result.stage_changed)
        self.assertIn("STAGE_SKIP_BLOCKED", result.reason_codes)

    def test_review_must_be_due_to_reach_retained(self) -> None:
        current = _current(
            stage=EvidenceStage.DEMONSTRATED,
            next_review_at=NOW + 3 * DAY,
        )
        evidence = _evidence(
            "evidence_earlyreview",
            relation=TaskRelation.REVIEW,
            occurred_at=NOW + DAY,
            source_sequence=2,
        )

        result = self.policy.project(_input(evidence, current=current))

        self.assertEqual(result.competency.evidence_stage, EvidenceStage.DEMONSTRATED)
        self.assertEqual(result.competency.next_review_at, current.next_review_at)
        self.assertIn("REVIEW_NOT_DUE", result.reason_codes)

    def test_failure_and_partial_outcomes_never_promote_or_downgrade(self) -> None:
        for outcome in (ProjectionOutcome.FAILED, ProjectionOutcome.PARTIAL):
            with self.subTest(outcome=outcome):
                current = _current(
                    stage=EvidenceStage.RETAINED,
                    next_review_at=NOW + 5 * DAY,
                )
                evidence = _evidence(
                    f"evidence_{outcome.value.lower()}001",
                    outcome=outcome,
                    occurred_at=NOW + timedelta(hours=1),
                    source_sequence=2,
                )

                result = self.policy.project(_input(evidence, current=current))

                self.assertEqual(result.competency.evidence_stage, EvidenceStage.RETAINED)
                self.assertEqual(
                    result.competency.next_review_at,
                    evidence.occurred_at + DAY,
                )
                self.assertIn("NON_SUCCESS_CANNOT_PROMOTE", result.reason_codes)


class LearnerProjectionAssistanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = LearnerProjectionPolicy()

    def test_high_assistance_full_solution_and_patch_each_block_promotion(self) -> None:
        cases = (
            (
                {"assistance_level": 3},
                "ASSISTANCE_BLOCKED_PROMOTION",
                "evidence_assisted01",
            ),
            (
                {"used_full_solution": True},
                "FULL_SOLUTION_BLOCKED_PROMOTION",
                "evidence_fullsolve1",
            ),
            (
                {"used_skill_patch": True},
                "SKILL_PATCH_BLOCKED_PROMOTION",
                "evidence_patchuse01",
            ),
        )
        for options, reason, evidence_id in cases:
            with self.subTest(reason=reason):
                evidence = _evidence(
                    evidence_id,
                    occurred_at=NOW + timedelta(hours=1),
                    source_sequence=2,
                    **options,
                )
                result = self.policy.project(_input(evidence, current=_current()))

                self.assertEqual(result.competency.evidence_stage, EvidenceStage.OBSERVED)
                self.assertFalse(result.stage_changed)
                self.assertIn(reason, result.reason_codes)
                self.assertEqual(
                    result.competency.next_review_at,
                    NOW + DAY,
                )

    def test_allowed_low_assistance_promotes_and_is_recorded(self) -> None:
        evidence = _evidence(
            "evidence_lowassist1",
            assistance_level=2,
            occurred_at=NOW + timedelta(hours=1),
            source_sequence=2,
        )

        result = self.policy.project(_input(evidence, current=_current()))

        self.assertEqual(result.competency.evidence_stage, EvidenceStage.DEMONSTRATED)
        self.assertEqual(result.competency.assistance_level, 2)


class LearnerProjectionReplayAndVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = LearnerProjectionPolicy()

    def test_same_explicit_input_has_exactly_equal_output(self) -> None:
        policy_input = _input(
            _evidence(
                "evidence_repeatable",
                occurred_at=NOW + timedelta(minutes=5),
                source_sequence=2,
            ),
            current=_current(),
        )

        first = self.policy.project(policy_input)
        second = self.policy.project(policy_input)

        self.assertEqual(first, second)
        self.assertEqual(
            first.learner_projection_policy_version,
            LEARNER_PROJECTION_POLICY_VERSION,
        )
        self.assertEqual(first.review_policy_version, REVIEW_POLICY_VERSION)

    def test_all_referenced_evidence_replay_is_idempotent(self) -> None:
        current = _current(
            evidence_ids=("evidence_replay0001", "evidence_replay0002"),
        )
        evidence = _evidence(
            "evidence_replay0002",
            "evidence_replay0001",
            occurred_at=NOW + timedelta(hours=1),
            source_sequence=2,
        )

        result = self.policy.project(_input(evidence, current=current))

        self.assertFalse(result.applied)
        self.assertFalse(result.stage_changed)
        self.assertIs(result.competency, current)
        self.assertEqual(result.reason_codes, ("EVIDENCE_ALREADY_PROJECTED",))

    def test_evidence_retention_is_bounded_and_replay_deterministic(self) -> None:
        def replay() -> CompetencyProjection:
            current: CompetencyProjection | None = None
            for index in range(70):
                evidence = _evidence(
                    f"evidence_retention{index:03d}",
                    outcome=ProjectionOutcome.FAILED,
                    occurred_at=NOW + timedelta(minutes=index),
                    source_sequence=index + 1,
                )
                current = self.policy.project(
                    _input(
                        evidence,
                        current=current,
                        learner_revision=index,
                    )
                ).competency
            return current

        first = replay()
        second = replay()

        self.assertEqual(first, second)
        self.assertEqual(len(first.evidence_ids), 64)
        self.assertEqual(first.evidence_ids[0], "evidence_retention006")
        self.assertEqual(first.evidence_ids[-1], "evidence_retention069")

    def test_partial_evidence_overlap_is_an_invariant_error(self) -> None:
        current = _current(evidence_ids=("evidence_overlap01",))
        evidence = _evidence(
            "evidence_overlap01",
            "evidence_newref001",
            occurred_at=NOW + timedelta(hours=1),
            source_sequence=2,
        )

        with self.assertRaises(LearnerProjectionPolicyError):
            self.policy.project(_input(evidence, current=current))

    def test_out_of_order_time_is_rejected(self) -> None:
        evidence = _evidence(
            "evidence_outoforder",
            occurred_at=NOW - timedelta(microseconds=1),
            source_sequence=2,
        )

        with self.assertRaises(LearnerProjectionPolicyError):
            self.policy.project(_input(evidence, current=_current()))

    def test_unknown_policy_versions_fail_closed(self) -> None:
        evidence = _evidence("evidence_versions01")
        with self.assertRaises(LearnerProjectionPolicyError):
            self.policy.project(_input(evidence, projection_version="projection_v999"))
        with self.assertRaises(LearnerProjectionPolicyError):
            self.policy.project(_input(evidence, review_version="review_v999"))

    def test_schedule_uses_only_explicit_evidence_time_and_version(self) -> None:
        first_time = NOW
        second_time = NOW + timedelta(days=30)

        first = self.policy.project(_input(_evidence("evidence_time00001", occurred_at=first_time)))
        second = self.policy.project(
            _input(_evidence("evidence_time00002", occurred_at=second_time))
        )

        self.assertEqual(first.competency.next_review_at - first_time, DAY)
        self.assertEqual(second.competency.next_review_at - second_time, DAY)

    def test_projection_types_are_immutable(self) -> None:
        evidence = _evidence("evidence_immutable1")
        result = self.policy.project(_input(evidence))

        with self.assertRaises(FrozenInstanceError):
            setattr(evidence, "assistance_level", 10)
        with self.assertRaises(FrozenInstanceError):
            setattr(result.competency, "evidence_stage", EvidenceStage.TRANSFERRED)


if __name__ == "__main__":
    unittest.main()
