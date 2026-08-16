"""Provider-neutral derivation of the one teaching outcome for a durable Run.

Storage adapters remain responsible for proving the Run, Evidence, World and
failure-history graph.  This module owns only the deterministic typed boundary
between those proven facts and the follow-up Agent role event.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from .domain import GameEvent, RunResultSnapshot, TaskSnapshot


class RunOutcomeInvariantError(ValueError):
    """Trusted Run facts cannot form one canonical follow-up event."""


def derive_run_outcome_event(
    *,
    root_event: GameEvent,
    run: RunResultSnapshot,
    task: TaskSnapshot,
    failure_count: int,
    occurred_at: datetime,
) -> GameEvent:
    """Derive ``run_failed`` or ``task_completed`` from already-proven facts.

    ``failure_count`` is the exact contiguous same-failure suffix through
    ``run``.  A persistence adapter must calculate and prove that suffix; this
    function deliberately cannot query storage or infer history by timestamp.
    """

    if not isinstance(root_event, GameEvent):
        raise TypeError("root_event must be a GameEvent")
    if not isinstance(run, RunResultSnapshot):
        raise TypeError("run must be a RunResultSnapshot")
    if not isinstance(task, TaskSnapshot):
        raise TypeError("task must be a TaskSnapshot")
    if isinstance(failure_count, bool) or not isinstance(failure_count, int):
        raise TypeError("failure_count must be an integer")
    if not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None:
        raise TypeError("occurred_at must be timezone-aware")
    if root_event.event_type != "run_skill_requested":
        raise RunOutcomeInvariantError("root event is not run_skill_requested")
    if (
        root_event.session_id != run.session_id
        or root_event.turn_id != run.turn_id
        or root_event.command_id != run.command_id
        or root_event.skill_ref != run.skill_ref
        or root_event.expected_world_revision != run.world_revision_before
    ):
        raise RunOutcomeInvariantError("root event and Run identity differ")
    task_actor = task.request_context.actor
    run_actor = run.request_context.actor
    if (
        task.task_id != root_event.task_id
        or not task.knowledge_points
        or (
            task_actor.tenant_id,
            task_actor.actor_id,
            task_actor.actor_type,
        )
        != (
            run_actor.tenant_id,
            run_actor.actor_id,
            run_actor.actor_type,
        )
        or task.request_context.content_ref != run.request_context.content_ref
    ):
        raise RunOutcomeInvariantError("Task cannot supply the Run outcome concept")
    if occurred_at < root_event.occurred_at:
        raise RunOutcomeInvariantError("Run durable time precedes its root event")

    if run.task_success:
        if failure_count != 0 or run.failure_key is not None:
            raise RunOutcomeInvariantError("successful Run has failure authority")
        event_type = "task_completed"
        failure_key = None
    else:
        if failure_count < 1 or run.failure_key is None:
            raise RunOutcomeInvariantError("failed Run lacks an exact failure suffix")
        event_type = "run_failed"
        failure_key = run.failure_key

    return GameEvent(
        event_id=_derived_event_id(root_event, run, event_type),
        event_type=event_type,
        student_id=root_event.student_id,
        task_id=root_event.task_id,
        session_id=root_event.session_id,
        turn_id=root_event.turn_id,
        command_id=root_event.command_id,
        occurred_at=occurred_at,
        expected_world_revision=run.world_revision_before,
        skill_ref=run.skill_ref,
        run_id=run.run_id,
        failure_count=failure_count,
        failure_key=failure_key,
        evidence_refs=run.evidence_refs,
        payload={"concept": task.knowledge_points[0]},
    )


def _derived_event_id(
    root_event: GameEvent,
    run: RunResultSnapshot,
    event_type: str,
) -> str:
    # This is the frozen A8 framing previously owned by the Agent PostgreSQL
    # adapter.  Keeping it here makes every authority adapter derive identical
    # replay identities without sharing a database schema.
    framed = "".join(
        f"{len(part)}:{part}"
        for part in (root_event.command_id, root_event.turn_id, run.run_id, event_type)
    )
    return f"evt_outcome_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}"


__all__ = [
    "RunOutcomeInvariantError",
    "derive_run_outcome_event",
]
