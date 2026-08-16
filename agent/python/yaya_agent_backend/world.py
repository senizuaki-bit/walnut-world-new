"""Deterministic watering rules that stage, but never persist, World changes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from yaya_agent_contracts import (
    ActionIntent,
    FrozenJsonObject,
    FrozenJsonValue,
    SkillRef,
    WaterIntent,
    WorldSnapshot,
    canonical_json_sha256,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


class WorldRuleViolation(ValueError):
    """A deterministic rejection that an application service can map to a Result."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = "WORLD_REVISION_CONFLICT" if retryable else "WORLD_RULE_REJECTED"
        self.reason = reason
        self.retryable = retryable


def _mutable_json(value: object, field_name: str = "state") -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        result: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise WorldRuleViolation(
                    f"{field_name} contains a non-string key",
                    reason="INVALID_WORLD_STATE",
                )
            result[key] = _mutable_json(item, f"{field_name}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [
            _mutable_json(item, f"{field_name}[{index}]") for index, item in enumerate(sequence)
        ]
    raise WorldRuleViolation(
        f"{field_name} contains a non-JSON value",
        reason="INVALID_WORLD_STATE",
    )


def _frozen_json(value: object) -> FrozenJsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return MappingProxyType({str(key): _frozen_json(item) for key, item in mapping.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return tuple(_frozen_json(item) for item in sequence)
    raise TypeError(f"cannot freeze non-JSON value {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class StagedWateringProposal:
    """Fully validated state proposal; no database write has happened yet."""

    world_id: str
    skill_ref: SkillRef
    revision_before: int
    revision_after: int
    sequence_before: int
    sequence_after: int
    state_hash: str
    staged_state: FrozenJsonObject
    intents: tuple[WaterIntent, ...]
    task_success: bool
    commit_eligible: bool
    failure_key: str | None
    watered_plots: int
    total_plots: int

    @property
    def world_difference(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "watered_plots": self.watered_plots,
                "total_plots": self.total_plots,
                "intent_count": len(self.intents),
            }
        )


class WateringWorldEngine:
    """Pure World rule engine for the eight-plot watering task."""

    def __init__(
        self,
        *,
        supported_rules_version: str = "farm-rules-1",
        expected_plot_count: int = 8,
        required_hydration: int = 100,
    ) -> None:
        if not 1 <= expected_plot_count <= 10_000:
            raise ValueError("expected_plot_count must be between 1 and 10000")
        if not 1 <= required_hydration <= 10_000:
            raise ValueError("required_hydration must be between 1 and 10000")
        if not supported_rules_version:
            raise ValueError("supported_rules_version is required")
        self._supported_rules_version = supported_rules_version
        self._expected_plot_count = expected_plot_count
        self._required_hydration = required_hydration

    def stage(
        self,
        snapshot: WorldSnapshot,
        skill_ref: SkillRef,
        intents: Sequence[ActionIntent],
    ) -> StagedWateringProposal:
        """Validate and stage watering actions on an isolated state copy."""

        if not isinstance(snapshot, WorldSnapshot):
            raise TypeError("snapshot must be a WorldSnapshot")
        if not isinstance(skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        if snapshot.world_rules_version != self._supported_rules_version:
            raise WorldRuleViolation(
                "World rules version is unsupported",
                reason="WORLD_RULES_VERSION_MISMATCH",
            )
        actual_hash = canonical_json_sha256(cast(Mapping[str, object], snapshot.state))
        if actual_hash != snapshot.state_hash:
            raise WorldRuleViolation(
                "World snapshot state hash does not match its state",
                reason="WORLD_STATE_HASH_MISMATCH",
            )
        if isinstance(intents, (str, bytes, bytearray)) or not isinstance(intents, Sequence):
            raise TypeError("intents must be a sequence")
        raw_intents = tuple(intents)
        if not raw_intents:
            raise WorldRuleViolation(
                "Watering requires at least one action intent",
                reason="EMPTY_ACTION_TRACE",
            )

        mutable = _mutable_json(snapshot.state)
        if not isinstance(mutable, dict):
            raise WorldRuleViolation(
                "World state must be an object",
                reason="INVALID_WORLD_STATE",
            )
        mutable_state = cast(dict[str, object], mutable)
        raw_avatar = mutable_state.get("avatar")
        raw_plots = mutable_state.get("plots")
        if not isinstance(raw_avatar, dict) or not isinstance(raw_plots, list):
            raise WorldRuleViolation(
                "World state is missing avatar or plots",
                reason="INVALID_WORLD_STATE",
            )
        avatar = cast(dict[str, object], raw_avatar)
        plots = cast(list[object], raw_plots)
        actor_entity_id = avatar.get("entity_id")
        if not isinstance(actor_entity_id, str) or not _IDENTIFIER.fullmatch(actor_entity_id):
            raise WorldRuleViolation(
                "World avatar entity identifier is invalid",
                reason="INVALID_WORLD_STATE",
            )
        if len(plots) != self._expected_plot_count:
            raise WorldRuleViolation(
                "Watering task World has an unexpected plot count",
                reason="INVALID_PLOT_COUNT",
            )

        plots_by_id: dict[str, dict[str, object]] = {}
        for raw_plot in plots:
            if not isinstance(raw_plot, dict):
                raise WorldRuleViolation(
                    "World plot must be an object",
                    reason="INVALID_WORLD_STATE",
                )
            plot = cast(dict[str, object], raw_plot)
            plot_id = plot.get("plot_id")
            hydration = plot.get("hydration")
            if (
                not isinstance(plot_id, str)
                or not _IDENTIFIER.fullmatch(plot_id)
                or isinstance(hydration, bool)
                or not isinstance(hydration, int)
                or not 0 <= hydration <= 10_000
            ):
                raise WorldRuleViolation(
                    "World plot identity or hydration is invalid",
                    reason="INVALID_WORLD_STATE",
                )
            if plot_id in plots_by_id:
                raise WorldRuleViolation(
                    "World contains duplicate plot identifiers",
                    reason="INVALID_WORLD_STATE",
                )
            plots_by_id[plot_id] = plot

        validated: list[WaterIntent] = []
        seen_intent_ids: set[str] = set()
        seen_plot_ids: set[str] = set()
        for raw_intent in raw_intents:
            if not isinstance(raw_intent, WaterIntent):
                raise WorldRuleViolation(
                    "Watering task accepts only WATER intents",
                    reason="UNSUPPORTED_ACTION_TYPE",
                )
            try:
                intent = WaterIntent(
                    intent_id=raw_intent.intent_id,
                    actor_entity_id=raw_intent.actor_entity_id,
                    expected_world_revision=raw_intent.expected_world_revision,
                    plot_id=raw_intent.plot_id,
                    amount_ml=raw_intent.amount_ml,
                )
            except (TypeError, ValueError) as error:
                raise WorldRuleViolation(
                    "Water intent violates the frozen action contract",
                    reason="INVALID_ACTION_INTENT",
                ) from error
            if raw_intent.action_type != "WATER":
                raise WorldRuleViolation(
                    "Water intent action_type was tampered with",
                    reason="INVALID_ACTION_INTENT",
                )
            if intent.intent_id in seen_intent_ids:
                raise WorldRuleViolation(
                    "Watering trace repeats an intent identifier",
                    reason="DUPLICATE_INTENT_ID",
                )
            if intent.plot_id in seen_plot_ids:
                raise WorldRuleViolation(
                    "Watering trace attempts to mutate a plot more than once",
                    reason="DUPLICATE_PLOT_ACTION",
                )
            if intent.expected_world_revision != snapshot.revision:
                raise WorldRuleViolation(
                    "Water intent targets an old World revision",
                    reason="WORLD_REVISION_CONFLICT",
                    retryable=True,
                )
            if intent.actor_entity_id != actor_entity_id:
                raise WorldRuleViolation(
                    "Water intent actor does not own this World avatar",
                    reason="ACTOR_ENTITY_MISMATCH",
                )
            plot = plots_by_id.get(intent.plot_id)
            if plot is None:
                raise WorldRuleViolation(
                    "Water intent targets a plot outside this World",
                    reason="PLOT_OUT_OF_RANGE",
                )
            hydration = cast(int, plot["hydration"])
            plot["hydration"] = min(10_000, hydration + intent.amount_ml)
            # One successful World CAS publishes one aggregate
            # ``world.committed`` event.  Every plot changed by that atomic
            # commit therefore points at the same durable stream sequence.
            plot["last_updated_event_sequence"] = snapshot.last_event_sequence + 1
            seen_intent_ids.add(intent.intent_id)
            seen_plot_ids.add(intent.plot_id)
            validated.append(intent)

        watered_plots = sum(
            cast(int, plot["hydration"]) >= self._required_hydration
            for plot in plots_by_id.values()
        )
        task_success = watered_plots == self._expected_plot_count
        frozen = _frozen_json(mutable_state)
        if not isinstance(frozen, Mapping):
            raise AssertionError("validated World state did not freeze as an object")
        staged_state = cast(FrozenJsonObject, frozen)
        state_hash = canonical_json_sha256(cast(Mapping[str, object], staged_state))
        return StagedWateringProposal(
            world_id=snapshot.world_id,
            skill_ref=skill_ref,
            revision_before=snapshot.revision,
            revision_after=snapshot.revision + (1 if task_success else 0),
            sequence_before=snapshot.last_event_sequence,
            sequence_after=(
                snapshot.last_event_sequence + 1 if task_success else snapshot.last_event_sequence
            ),
            state_hash=state_hash,
            staged_state=staged_state,
            intents=tuple(validated),
            task_success=task_success,
            commit_eligible=task_success,
            failure_key=None if task_success else "watering_loop_short",
            watered_plots=watered_plots,
            total_plots=self._expected_plot_count,
        )


__all__ = ["StagedWateringProposal", "WateringWorldEngine", "WorldRuleViolation"]
