"""The normal workflow worker must yield after learner hand-off commit loss."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from walnut_backend.adapters.postgres.workflow_jobs import WorkflowFenceLost
from walnut_backend.workers.workflow_worker import WorkflowWorker


def test_turn_handoff_ack_loss_does_not_requeue_waiting_parent() -> None:
    claim = cast(
        Any,
        SimpleNamespace(
            operation="EXECUTE_AGENT_TURN",
            tenant_id="tenant_yaya",
            job_id="job_turn_handoff_01",
        ),
    )

    class _Jobs:
        async def claim_next(self, **kwargs: Any) -> Any:
            del kwargs
            return claim

    class _Handler:
        operations = frozenset({"EXECUTE_AGENT_TURN"})

        async def execute(self, claim: Any) -> None:
            assert claim is not None
            raise ConnectionError("learner hand-off acknowledgement lost")

    worker = WorkflowWorker(
        session_factory=cast(Any, object()),
        jobs=cast(Any, _Jobs()),
        commands=cast(Any, object()),
        handlers=(_Handler(),),
        worker_id="turn-worker-01",
    )
    recorded = 0

    async def record_unexpected(*args: Any, **kwargs: Any) -> None:
        nonlocal recorded
        del args, kwargs
        recorded += 1
        raise WorkflowFenceLost("parent is already WAITING_PROJECTION")

    worker._record_unexpected = record_unexpected  # pyright: ignore[reportPrivateUsage]
    assert asyncio.run(worker.run_once("tenant_yaya")) is True
    assert recorded == 1
