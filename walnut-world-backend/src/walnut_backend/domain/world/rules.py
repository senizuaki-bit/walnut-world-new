"""Explicit, versioned rules supplied by an activated content version."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorldRules:
    """A fully materialized ruleset; content loading is outside the pure domain."""

    content_version: str
    max_actions: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    harvest_growth_stage: int
    success_score: int

    def __post_init__(self) -> None:
        if not self.content_version:
            raise ValueError("content_version must not be empty")
        if self.max_actions < 1:
            raise ValueError("max_actions must be positive")
        if self.min_x > self.max_x or self.min_y > self.max_y:
            raise ValueError("world bounds must be ordered")
        if self.harvest_growth_stage < 0:
            raise ValueError("harvest_growth_stage must be non-negative")
        if self.success_score < 0:
            raise ValueError("success_score must be non-negative")

    def contains(self, x: int, y: int) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y
