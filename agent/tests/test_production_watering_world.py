from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.world import (  # noqa: E402
    WateringWorldEngine,
    WorldRuleViolation,
)
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    ContentRef,
    OperationContext,
    SkillRef,
    SpeakIntent,
    WaterIntent,
    WorldSnapshot,
    canonical_json_sha256,
)


def _operation() -> OperationContext:
    now = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    return OperationContext(
        request_id="req_world_0001",
        correlation_id="corr_world_0001",
        trace_id="trace_world_0001",
        requested_at=now,
        actor=ActorRef(
            tenant_id="tenant_yaya",
            actor_id="student_0001",
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        ),
        content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
        command_id="cmd_world_0001",
        causation_id=None,
    )


def _state() -> dict[str, object]:
    return {
        "clock": {"day": 1, "minute_of_day": 480, "tick": 10},
        "avatar": {
            "entity_id": "avatar_0001",
            "position": {"x": 0, "y": 0},
            "energy": 100,
        },
        "inventory": [],
        "plots": [
            {
                "plot_id": f"plot_{index:04d}",
                "position": {"x": index, "y": 0},
                "soil_state": "TILLED",
                "hydration": 0,
                "crop": None,
                "last_updated_event_sequence": 0,
            }
            for index in range(1, 9)
        ],
        "agents": [],
    }


def _snapshot(
    state: dict[str, object] | None = None, *, hash_override: str | None = None
) -> WorldSnapshot:
    operation = _operation()
    value = _state() if state is None else state
    return WorldSnapshot(
        request_context=operation,
        world_id="world_watering_0001",
        revision=5,
        last_event_sequence=40,
        state_hash=hash_override or canonical_json_sha256(value),
        generated_at=operation.requested_at,
        world_rules_version="farm-rules-1",
        state=value,
    )


def _skill_ref() -> SkillRef:
    return SkillRef(
        skill_id="skill_watering_0001",
        skill_version_id="skill_version_0001",
        artifact_sha256="b" * 64,
        certification_id="certification_0001",
    )


def _water(index: int, *, intent_index: int | None = None, revision: int = 5) -> WaterIntent:
    identity = index if intent_index is None else intent_index
    return WaterIntent(
        intent_id=f"intent_water_{identity:04d}",
        actor_entity_id="avatar_0001",
        expected_world_revision=revision,
        plot_id=f"plot_{index:04d}",
        amount_ml=100,
    )


class WateringWorldEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = WateringWorldEngine()

    def test_seven_of_eight_is_incomplete_and_input_world_is_unchanged(self) -> None:
        raw_state = _state()
        proposal = self.engine.stage(
            _snapshot(raw_state),
            _skill_ref(),
            tuple(_water(index) for index in range(1, 8)),
        )
        self.assertFalse(proposal.task_success)
        self.assertFalse(proposal.commit_eligible)
        self.assertEqual(proposal.failure_key, "watering_loop_short")
        self.assertEqual(proposal.watered_plots, 7)
        self.assertEqual(proposal.revision_after, 5)
        self.assertEqual(proposal.sequence_after, 40)
        self.assertTrue(all(plot["hydration"] == 0 for plot in raw_state["plots"]))
        self.assertEqual(
            sum(plot["hydration"] == 100 for plot in proposal.staged_state["plots"]),
            7,
        )

    def test_eight_of_eight_is_a_single_revision_staged_proposal(self) -> None:
        proposal = self.engine.stage(
            _snapshot(),
            _skill_ref(),
            tuple(_water(index) for index in range(1, 9)),
        )
        self.assertTrue(proposal.task_success)
        self.assertTrue(proposal.commit_eligible)
        self.assertIsNone(proposal.failure_key)
        self.assertEqual(proposal.watered_plots, 8)
        self.assertEqual((proposal.revision_before, proposal.revision_after), (5, 6))
        self.assertEqual((proposal.sequence_before, proposal.sequence_after), (40, 41))
        self.assertTrue(
            all(
                plot["last_updated_event_sequence"] == 41 for plot in proposal.staged_state["plots"]
            )
        )
        self.assertEqual(canonical_json_sha256(proposal.staged_state), proposal.state_hash)
        with self.assertRaises(TypeError):
            proposal.staged_state["plots"] = ()

    def test_duplicate_out_of_range_stale_wrong_actor_and_non_water_are_rejected(self) -> None:
        duplicate_plot = (_water(1), _water(1, intent_index=2))
        unknown_plot = (_water(9),)
        stale = (_water(1, revision=4),)
        wrong_actor = (
            WaterIntent(
                intent_id="intent_wrong_actor_0001",
                actor_entity_id="avatar_other_0001",
                expected_world_revision=5,
                plot_id="plot_0001",
                amount_ml=100,
            ),
        )
        non_water = (
            SpeakIntent(
                intent_id="intent_speak_0001",
                actor_entity_id="avatar_0001",
                expected_world_revision=5,
                text="I watered everything.",
                audience="LEARNER",
            ),
        )
        cases = (
            (duplicate_plot, "DUPLICATE_PLOT_ACTION", False),
            (unknown_plot, "PLOT_OUT_OF_RANGE", False),
            (stale, "WORLD_REVISION_CONFLICT", True),
            (wrong_actor, "ACTOR_ENTITY_MISMATCH", False),
            (non_water, "UNSUPPORTED_ACTION_TYPE", False),
        )
        for intents, reason, retryable in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(WorldRuleViolation) as caught:
                    self.engine.stage(_snapshot(), _skill_ref(), intents)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(caught.exception.retryable, retryable)

    def test_duplicate_intent_and_tampered_typed_intent_are_rejected(self) -> None:
        with self.assertRaises(WorldRuleViolation) as duplicate:
            self.engine.stage(
                _snapshot(),
                _skill_ref(),
                (_water(1), _water(2, intent_index=1)),
            )
        self.assertEqual(duplicate.exception.reason, "DUPLICATE_INTENT_ID")

        tampered = _water(1)
        object.__setattr__(tampered, "amount_ml", 0)
        with self.assertRaises(WorldRuleViolation) as invalid:
            self.engine.stage(_snapshot(), _skill_ref(), (tampered,))
        self.assertEqual(invalid.exception.reason, "INVALID_ACTION_INTENT")

    def test_snapshot_hash_is_recomputed_before_any_action_is_staged(self) -> None:
        with self.assertRaises(WorldRuleViolation) as caught:
            self.engine.stage(
                _snapshot(hash_override="d" * 64),
                _skill_ref(),
                (_water(1),),
            )
        self.assertEqual(caught.exception.reason, "WORLD_STATE_HASH_MISMATCH")


if __name__ == "__main__":
    unittest.main()
