"""Pure, deterministic world-rule coverage."""

from __future__ import annotations

from yaya_agent_contracts import (
    HarvestIntent,
    MoveIntent,
    PlantIntent,
    WaterIntent,
    WorldPosition,
)

from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.domain.world.state import WorldRuleViolation


def test_same_state_rules_and_intents_produce_identical_transition() -> None:
    engine = WorldEngine()
    rules = WorldRules(
        content_version="1.0.0",
        max_actions=4,
        min_x=0,
        max_x=4,
        min_y=0,
        max_y=4,
        harvest_growth_stage=2,
        success_score=3,
    )
    intents = (
        MoveIntent("intent_move_0001", "avatar_0001", 7, WorldPosition(2, 1)),
        PlantIntent("intent_plant_001", "avatar_0001", 7, "plot_0001", "tomato"),
        WaterIntent("intent_water_001", "avatar_0001", 7, "plot_0001", 500),
    )

    first = engine.apply(snapshot_state(tilled=True), intents, rules)
    second = engine.apply(snapshot_state(tilled=True), intents, rules)

    assert first == second
    assert first.state["avatar"]["position"] == {"x": 2, "y": 1}
    assert first.applied_intent_ids == tuple(intent.intent_id for intent in intents)


def test_rejects_out_of_bounds_move_and_duplicate_action() -> None:
    engine = WorldEngine()
    rules = ruleset()

    try:
        engine.apply(
            snapshot_state(),
            (MoveIntent("intent_move_0002", "avatar_0001", 7, WorldPosition(5, 1)),),
            rules,
        )
    except WorldRuleViolation as error:
        assert error.code == "MOVE_OUT_OF_BOUNDS"
    else:  # pragma: no cover - keeps the expected contract failure explicit
        raise AssertionError("out-of-bounds movement must be rejected")

    action = MoveIntent("intent_move_0003", "avatar_0001", 7, WorldPosition(2, 1))
    try:
        engine.apply(snapshot_state(), (action, action), rules)
    except WorldRuleViolation as error:
        assert error.code == "DUPLICATE_ACTION"
    else:  # pragma: no cover
        raise AssertionError("duplicate actions must be rejected")


def test_rejects_illegal_water_and_action_limit() -> None:
    engine = WorldEngine()
    rules = ruleset(max_actions=1)

    try:
        engine.apply(
            snapshot_state(),
            (WaterIntent("intent_water_002", "avatar_0001", 7, "plot_0001", 1),),
            rules,
        )
    except WorldRuleViolation as error:
        assert error.code == "ILLEGAL_WATER"
    else:  # pragma: no cover
        raise AssertionError("watering an untilled plot must be rejected")

    actions = (
        MoveIntent("intent_move_0004", "avatar_0001", 7, WorldPosition(2, 1)),
        MoveIntent("intent_move_0005", "avatar_0001", 7, WorldPosition(3, 1)),
    )
    try:
        engine.apply(snapshot_state(), actions, rules)
    except WorldRuleViolation as error:
        assert error.code == "ACTION_LIMIT_EXCEEDED"
    else:  # pragma: no cover
        raise AssertionError("action limit must be rejected")


def test_content_version_rules_control_score_and_success_condition() -> None:
    engine = WorldEngine()
    state = snapshot_state(ready_crop=True)
    harvest = HarvestIntent("intent_harvest_01", "avatar_0001", 7, "plot_0001")

    incomplete = engine.apply(state, (harvest,), ruleset(success_score=2))
    complete = engine.apply(state, (harvest,), ruleset(success_score=1))

    assert incomplete.score == 1
    assert incomplete.success is False
    assert complete.score == 1
    assert complete.success is True


def ruleset(*, max_actions: int = 4, success_score: int = 1) -> WorldRules:
    return WorldRules(
        content_version="1.0.0",
        max_actions=max_actions,
        min_x=0,
        max_x=4,
        min_y=0,
        max_y=4,
        harvest_growth_stage=2,
        success_score=success_score,
    )


def snapshot_state(*, ready_crop: bool = False, tilled: bool = False) -> dict[str, object]:
    return {
        "clock": {"day": 1, "minute_of_day": 0, "tick": 7},
        "avatar": {
            "entity_id": "avatar_0001",
            "position": {"x": 1, "y": 1},
            "energy": 100,
        },
        "inventory": [{"item_id": "seed.tomato", "quantity": 1}],
        "plots": [
            {
                "plot_id": "plot_0001",
                "position": {"x": 2, "y": 1},
                "soil_state": "TILLED" if tilled or ready_crop else "UNTILLED",
                "hydration": 0,
                "crop": (
                    {
                        "crop_type": "tomato",
                        "growth_stage": 2,
                        "planted_at_tick": 1,
                        "ready_to_harvest": True,
                    }
                    if ready_crop
                    else None
                ),
                "last_updated_event_sequence": 0,
            }
        ],
        "agents": [],
    }
