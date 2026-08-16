"""Deterministic score and success evaluation for a materialized ruleset."""

from __future__ import annotations

from collections.abc import Iterable

from .rules import WorldRules


def score_actions(action_types: Iterable[str]) -> int:
    """Only harvest produces score; the content ruleset sets the completion threshold."""
    return sum(1 for action_type in action_types if action_type == "HARVEST")


def is_successful(score: int, rules: WorldRules) -> bool:
    return score >= rules.success_score
