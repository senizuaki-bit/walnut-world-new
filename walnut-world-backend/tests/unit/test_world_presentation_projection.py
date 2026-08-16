"""Focused red tests for authoritative World presentation reducer output."""

from __future__ import annotations

from yaya_agent_contracts import HarvestIntent, MoveIntent, WorldPosition

from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules


def test_harvest_steps_are_closed_from_authoritative_reducer_state() -> None:
    state = _harvest_state()
    intents = (
        HarvestIntent("intent_harvest_0001", "avatar_0001", 0, "plot_0001"),
        HarvestIntent("intent_harvest_0002", "avatar_0001", 0, "plot_0002"),
    )

    first = WorldEngine().apply(state, intents, _rules())
    second = WorldEngine().apply(state, intents, _rules())

    assert first == second
    assert len(first.reducer_steps) == 2
    one, two = first.reducer_steps
    assert one.intent_id == "intent_harvest_0001"
    assert one.action_type == "HARVEST"
    assert one.state_hash_before != one.state_hash_after
    assert one.state_hash_after == two.state_hash_before
    assert two.state_hash_after == first.state_hash
    assert one.harvest is not None
    assert one.harvest.actor_entity_id == "avatar_0001"
    assert one.harvest.plot_id == "plot_0001"
    assert (one.harvest.position_x, one.harvest.position_y) == (1, 0)
    assert one.harvest.crop_type == "tomato"
    assert one.harvest.growth_stage == 2
    assert one.harvest.ready_to_harvest is True
    assert two.harvest is not None
    assert two.harvest.plot_id == "plot_0002"
    assert (two.harvest.position_x, two.harvest.position_y) == (2, 0)

    # The display projection is captured before the reducer removes the crop,
    # but the original caller-owned state remains untouched.
    assert first.state["plots"][0]["crop"] is None
    assert state["plots"][0]["crop"] is not None


def test_non_harvest_step_keeps_hash_closure_without_harvest_payload() -> None:
    transition = WorldEngine().apply(
        _harvest_state(),
        (MoveIntent("intent_move_0001", "avatar_0001", 0, WorldPosition(3, 1)),),
        _rules(success_score=0),
    )

    assert transition.success is True
    assert len(transition.reducer_steps) == 1
    step = transition.reducer_steps[0]
    assert step.action_type == "MOVE"
    assert step.harvest is None
    assert step.state_hash_before != step.state_hash_after
    assert step.state_hash_after == transition.state_hash


def _rules(*, success_score: int = 2) -> WorldRules:
    return WorldRules(
        content_version="1.0.0",
        max_actions=8,
        min_x=0,
        max_x=31,
        min_y=0,
        max_y=31,
        harvest_growth_stage=2,
        success_score=success_score,
    )


def _harvest_state() -> dict[str, object]:
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
                "crop": {
                    "crop_type": "tomato",
                    "growth_stage": 2,
                    "planted_at_tick": 10,
                    "ready_to_harvest": True,
                },
                "last_updated_event_sequence": 0,
            }
            for index in range(1, 3)
        ],
        "agents": [],
    }
