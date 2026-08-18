"""Deterministic score and success evaluation for a materialized ruleset."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .rules import WorldRules


def score_actions(action_types: Iterable[str]) -> int:
    """Only harvest produces score; the content ruleset sets the completion threshold."""
    return sum(1 for action_type in action_types if action_type == "HARVEST")


def score_watering(plots: Sequence[Mapping[str, object]], rules: WorldRules) -> int:
    """Count plots whose watered amount (hydration) equals the expected units.

    Plots are evaluated in snapshot order against ``rules.watering_expected_units``.
    A plot is correct when its hydration matches the expected watering decision for
    that plot index (0 units means the student must skip watering it)."""
    expected = rules.watering_expected_units
    if expected is None:
        return 0
    correct = 0
    for index, plot in enumerate(plots):
        if index >= len(expected):
            break
        hydration = plot.get("hydration")
        if isinstance(hydration, int) and hydration == expected[index]:
            correct += 1
    return correct


def is_successful(score: int, rules: WorldRules) -> bool:
    return score >= rules.success_score
