"""Stable cross-client hashing and corruption checks for presentation events."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yaya_agent_contracts import HarvestIntent

from walnut_backend.adapters.postgres.world_presentation import (
    build_harvest_presentation_event,
    validate_presentation_event_data,
)
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules

AGENT_ROOT = Path(__file__).resolve().parents[3] / "agent"


def test_event_identity_and_hashes_are_stable_fixed_order_values() -> None:
    event = _event()
    replay = _event()

    assert event == replay
    assert event["event_id"] == f"presentation_{event['integrity_sha256'][:32]}"
    assert event["event_type"] == "world.action.harvested"
    assert event["stream_id"] == "world-presentation:world_0001"
    assert event["sequence"] == 1
    assert event["action_index"] == 0
    assert event["action_count"] == 1
    assert set(event["payload"]) == {
        "actor_entity_id",
        "plot_id",
        "position",
        "crop_type",
        "growth_stage",
        "ready_to_harvest",
    }
    assert validate_presentation_event_data(event) == event


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("event_type", "world.action.unknown"),
        ("sequence", 2),
        ("state_hash_after", "f" * 64),
        ("payload_sha256", "e" * 64),
        ("integrity_sha256", "d" * 64),
    ),
)
def test_event_envelope_tampering_fails_closed(field: str, replacement: object) -> None:
    event = copy.deepcopy(_event())
    event[field] = replacement

    with pytest.raises(ValueError):
        validate_presentation_event_data(event)


def test_payload_and_extra_key_tampering_fail_closed() -> None:
    payload_tamper = copy.deepcopy(_event())
    payload_tamper["payload"]["crop_type"] = "forged"
    with pytest.raises(ValueError):
        validate_presentation_event_data(payload_tamper)

    extra = copy.deepcopy(_event())
    extra["raw_intent"] = {"animation": "trust-me"}
    with pytest.raises(ValueError):
        validate_presentation_event_data(extra)


def test_agent_golden_example_matches_backend_hash_algorithm() -> None:
    example = json.loads(
        (AGENT_ROOT / "contracts/examples/game-world-presentation-event-page.json").read_text(
            encoding="utf-8"
        )
    )["value"]
    first, second = example["events"]

    assert validate_presentation_event_data(first) == first
    assert validate_presentation_event_data(second) == second
    assert first["payload_sha256"] == (
        "33763d5208fee9dca8f626fe5d588b95bbd5250c8d2286b730ddeb673fc3130d"
    )
    assert first["integrity_sha256"] == (
        "0c157495c7b085330271f82b321e3f38443083616f24d9aba493b2d2e46dc97c"
    )
    assert second["integrity_sha256"] == (
        "67f97c3cb795c503f7a38390184e82557998516a552477bbddeef0c9ab01289c"
    )


def _event() -> dict[str, object]:
    state = {
        "clock": {"day": 1, "minute_of_day": 480, "tick": 10},
        "avatar": {"entity_id": "avatar_0001", "position": {"x": 0, "y": 0}, "energy": 100},
        "inventory": [],
        "plots": [
            {
                "plot_id": "plot_0001",
                "position": {"x": 1, "y": 0},
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
        ],
        "agents": [],
    }
    transition = WorldEngine().apply(
        state,
        (HarvestIntent("intent_harvest_0001", "avatar_0001", 0, "plot_0001"),),
        WorldRules("1.0.0", 8, 0, 31, 0, 31, 2, 1),
    )
    return build_harvest_presentation_event(
        step=transition.reducer_steps[0],
        stream_id="world-presentation:world_0001",
        sequence=1,
        occurred_at=datetime(2026, 8, 14, 1, 2, 3, 456789, tzinfo=UTC),
        tenant_id="tenant_0001",
        session_id="session_0001",
        turn_id="turn_0001",
        command_id="cmd_command_0001",
        run_id="run_0001",
        world_id="world_0001",
        commit_id="commit_world_0001",
        world_revision=1,
        action_index=0,
        action_count=1,
        final_world_event_sequence=1,
        final_snapshot_state_hash=transition.state_hash,
    )
