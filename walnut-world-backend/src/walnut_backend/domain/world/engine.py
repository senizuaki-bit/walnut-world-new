"""Pure deterministic reducer from contract ActionIntent values to world state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from yaya_agent_contracts import (
    ActionIntent,
    HarvestIntent,
    InteractIntent,
    MoveIntent,
    PlantIntent,
    SpeakIntent,
    WaterIntent,
)

from .rules import WorldRules
from .scoring import is_successful, score_actions, score_watering
from .state import WorldRuleViolation, mutable_state, state_hash


@dataclass(frozen=True, slots=True)
class HarvestPresentation:
    """Closed HARVEST facts captured from the authoritative pre-reducer state."""

    actor_entity_id: str
    plot_id: str
    position_x: int
    position_y: int
    crop_type: str
    growth_stage: int
    ready_to_harvest: bool


@dataclass(frozen=True, slots=True)
class WorldReducerStep:
    """One deterministic reducer step; only HARVEST has a display projection in INT2."""

    intent_id: str
    action_type: str
    state_hash_before: str
    state_hash_after: str
    harvest: HarvestPresentation | None


@dataclass(frozen=True, slots=True)
class WorldTransition:
    state: Mapping[str, Any]
    applied_intent_ids: tuple[str, ...]
    reducer_steps: tuple[WorldReducerStep, ...]
    score: int
    success: bool
    state_hash: str


class WorldEngine:
    """No I/O, clock, randomness, ORM or sandbox dependency is allowed here."""

    def apply(
        self,
        state: Mapping[str, Any],
        intents: Sequence[ActionIntent],
        rules: WorldRules,
    ) -> WorldTransition:
        if len(intents) > rules.max_actions:
            raise WorldRuleViolation("ACTION_LIMIT_EXCEEDED", "content action limit exceeded")
        next_state = mutable_state(state)
        avatar = _mapping(next_state.get("avatar"), "avatar")
        actor_id = avatar.get("entity_id")
        intent_ids: set[str] = set()
        action_types: list[str] = []
        reducer_steps: list[WorldReducerStep] = []
        for intent in intents:
            if intent.intent_id in intent_ids:
                raise WorldRuleViolation("DUPLICATE_ACTION", "intent_id may be applied once per transition")
            intent_ids.add(intent.intent_id)
            if intent.actor_entity_id != actor_id:
                raise WorldRuleViolation("ACTOR_MISMATCH", "intent actor is not the snapshot avatar")
            before_hash = state_hash(next_state)
            harvest = (
                _harvest_presentation(next_state, intent)
                if isinstance(intent, HarvestIntent)
                else None
            )
            self._apply_intent(next_state, intent, rules)
            after_hash = state_hash(next_state)
            reducer_steps.append(
                WorldReducerStep(
                    intent_id=intent.intent_id,
                    action_type=intent.action_type,
                    state_hash_before=before_hash,
                    state_hash_after=after_hash,
                    harvest=harvest,
                )
            )
            action_types.append(intent.action_type)

        score = (
            score_watering(
                cast(Sequence[Mapping[str, object]], next_state.get("plots", [])),
                rules,
            )
            if rules.is_watering
            else score_actions(action_types)
        )
        return WorldTransition(
            state=next_state,
            applied_intent_ids=tuple(intent.intent_id for intent in intents),
            reducer_steps=tuple(reducer_steps),
            score=score,
            success=is_successful(score, rules),
            state_hash=state_hash(next_state),
        )

    def _apply_intent(
        self, state: dict[str, Any], intent: ActionIntent, rules: WorldRules
    ) -> None:
        if isinstance(intent, MoveIntent):
            if not rules.contains(intent.destination.x, intent.destination.y):
                raise WorldRuleViolation("MOVE_OUT_OF_BOUNDS", "destination is outside content bounds")
            state["avatar"]["position"] = {"x": intent.destination.x, "y": intent.destination.y}
            return
        if isinstance(intent, PlantIntent):
            plot = _plot(state, intent.plot_id)
            if plot["soil_state"] != "TILLED" or plot["crop"] is not None:
                raise WorldRuleViolation("ILLEGAL_PLANT", "planting requires an empty tilled plot")
            plot["crop"] = {
                "crop_type": intent.crop_type,
                "growth_stage": 0,
                "planted_at_tick": state["clock"]["tick"],
                "ready_to_harvest": False,
            }
            return
        if isinstance(intent, WaterIntent):
            plot = _plot(state, intent.plot_id)
            if plot["soil_state"] != "TILLED" or plot["crop"] is None:
                raise WorldRuleViolation("ILLEGAL_WATER", "watering requires a planted tilled plot")
            plot["hydration"] = min(10_000, plot["hydration"] + intent.amount_ml)
            crop = _mapping(plot["crop"], "crop")
            crop["growth_stage"] = min(100, crop["growth_stage"] + 1)
            crop["ready_to_harvest"] = crop["growth_stage"] >= rules.harvest_growth_stage
            plot["crop"] = crop
            return
        if isinstance(intent, HarvestIntent):
            plot = _plot(state, intent.plot_id)
            crop = plot["crop"]
            if not isinstance(crop, Mapping) or not crop["ready_to_harvest"]:
                raise WorldRuleViolation("ILLEGAL_HARVEST", "harvest requires a ready crop")
            plot["crop"] = None
            return
        if isinstance(intent, InteractIntent):
            if not any(agent["entity_id"] == intent.target_entity_id for agent in state["agents"]):
                raise WorldRuleViolation("UNKNOWN_INTERACTION_TARGET", "interaction target is not in the world")
            return
        if isinstance(intent, SpeakIntent):
            return
        raise WorldRuleViolation("UNSUPPORTED_ACTION", "ActionIntent variant is not supported")


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorldRuleViolation("INVALID_WORLD_STATE", f"{field} is not an object")
    return dict(value)


def _plot(state: Mapping[str, Any], plot_id: str) -> dict[str, Any]:
    plots = state.get("plots")
    if not isinstance(plots, list):
        raise WorldRuleViolation("INVALID_WORLD_STATE", "plots is not an array")
    for plot in plots:
        if isinstance(plot, dict) and plot.get("plot_id") == plot_id:
            return plot
    raise WorldRuleViolation("UNKNOWN_PLOT", "plot does not exist in the snapshot")


def _harvest_presentation(
    state: Mapping[str, Any], intent: HarvestIntent
) -> HarvestPresentation:
    plot = _plot(state, intent.plot_id)
    position = plot.get("position")
    crop = plot.get("crop")
    if not isinstance(position, Mapping) or not isinstance(crop, Mapping):
        raise WorldRuleViolation(
            "INVALID_WORLD_STATE", "harvest presentation requires plot position and crop"
        )
    x = position.get("x")
    y = position.get("y")
    crop_type = crop.get("crop_type")
    growth_stage = crop.get("growth_stage")
    ready = crop.get("ready_to_harvest")
    if (
        not isinstance(x, int)
        or isinstance(x, bool)
        or not isinstance(y, int)
        or isinstance(y, bool)
        or not isinstance(crop_type, str)
        or not crop_type
        or not isinstance(growth_stage, int)
        or isinstance(growth_stage, bool)
        or not isinstance(ready, bool)
    ):
        raise WorldRuleViolation(
            "INVALID_WORLD_STATE", "harvest presentation fields are malformed"
        )
    return HarvestPresentation(
        actor_entity_id=intent.actor_entity_id,
        plot_id=intent.plot_id,
        position_x=x,
        position_y=y,
        crop_type=crop_type,
        growth_stage=growth_stage,
        ready_to_harvest=ready,
    )
