"""Deterministic, exhaustive Agent routing and hint policy."""

from __future__ import annotations

from .domain import BUG_FAILURE_THRESHOLD, GameEvent, RoleRoute


class RoleRouter:
    def __init__(self, *, bug_failure_threshold: int = BUG_FAILURE_THRESHOLD) -> None:
        if bug_failure_threshold != BUG_FAILURE_THRESHOLD:
            raise ValueError("bug_failure_threshold is frozen at 3 for replay compatibility")
        self._bug_failure_threshold = bug_failure_threshold

    def route(self, event: GameEvent) -> RoleRoute:
        event_type = event.event_type
        if event_type == "task_started":
            return RoleRoute(
                event_type, "world_agent", "task events are introduced by the world role"
            )
        if event_type == "run_skill_requested":
            return RoleRoute(
                event_type, "xiaohutao", "skill execution belongs to the AI apprentice"
            )
        if event_type == "compile_failed":
            return RoleRoute(event_type, "teaching_agent", "compile failures always use teaching")
        if event_type == "run_failed":
            role = (
                "bug_agent"
                if event.failure_count >= self._bug_failure_threshold
                else "teaching_agent"
            )
            return RoleRoute(event_type, role, "same-failure threshold applied")
        if event_type == "hint_requested":
            role = (
                "bug_agent"
                if event.failure_count >= self._bug_failure_threshold
                else "teaching_agent"
            )
            return RoleRoute(event_type, role, "hint uses the same-failure threshold")
        if event_type == "skill_patch_requested":
            return RoleRoute(
                event_type,
                "teaching_agent",
                "explicit Patch requests always stay inside the teaching role",
            )
        if event_type == "task_completed":
            return RoleRoute(event_type, "book_agent", "completed tasks produce a growth summary")
        if event_type == "compile_succeeded":
            return RoleRoute(event_type, None, "compile success is an objective system fact")
        if event_type == "run_succeeded":
            return RoleRoute(event_type, None, "run success awaits an explicit completion event")
        if event_type == "skill_patch_confirmed":
            return RoleRoute(
                event_type, None, "patch application is a deterministic product workflow"
            )
        raise AssertionError(f"unreachable validated event type: {event_type}")


def calculate_hint_level(
    failure_count: int,
    *,
    requested_hint: bool,
    maximum: int = 4,
) -> int:
    if isinstance(failure_count, bool) or failure_count < 0:
        raise ValueError("failure_count must be a non-negative integer")
    if not isinstance(requested_hint, bool):
        raise ValueError("requested_hint must be boolean")
    if isinstance(maximum, bool) or not 0 <= maximum <= 4:
        raise ValueError("maximum must be an integer between 0 and 4")
    if failure_count <= 1:
        level = 0
    elif failure_count <= 2:
        level = 1
    elif failure_count <= 4:
        level = 2
    else:
        level = 3
    if requested_hint:
        level += 1
    return min(level, maximum)


__all__ = ["RoleRouter", "calculate_hint_level"]
