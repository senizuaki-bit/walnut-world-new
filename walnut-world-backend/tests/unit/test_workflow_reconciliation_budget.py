"""Provider reconciliation waits are outside the normal failure budget."""

from __future__ import annotations

import json

from yaya_agent_runtime import (
    AgentContextError,
    RuntimeBoundaryError,
    RuntimeBoundaryStage,
)

from walnut_backend.adapters.postgres.workflow_jobs import (
    WorkflowBoundaryError,
    WorkflowInvariantError,
    WorkflowReconciliationPending,
    WorkflowRetryableError,
)
from walnut_backend.workers.turn_worker import _final_runtime_boundary
from walnut_backend.workers.workflow_worker import (
    _failure_budget_exhausted,
    _sanitized_failure,
)


def test_slow_provider_polling_never_exhausts_normal_failure_budget() -> None:
    pending = WorkflowReconciliationPending("provider is pending", retry_after_seconds=1)
    assert all(
        not _failure_budget_exhausted(
            pending,
            previous_failures=normal_failures,
            maximum_attempts=5,
        )
        for normal_failures in range(0, 100)
    )


def test_only_normal_failures_advance_the_bounded_budget() -> None:
    failure = WorkflowRetryableError("ordinary transient failure")
    assert not _failure_budget_exhausted(
        failure,
        previous_failures=3,
        maximum_attempts=5,
    )
    assert _failure_budget_exhausted(
        failure,
        previous_failures=4,
        maximum_attempts=5,
    )


def test_durable_invariant_fails_once_without_changing_boundary_budget() -> None:
    invariant = WorkflowInvariantError("durable authority is corrupt")
    assert _failure_budget_exhausted(
        invariant,
        previous_failures=0,
        maximum_attempts=5,
    )

    boundary = WorkflowBoundaryError("OUTCOME_AUTHORITY")
    assert not _failure_budget_exhausted(
        boundary,
        previous_failures=0,
        maximum_attempts=5,
    )
    assert _failure_budget_exhausted(
        boundary,
        previous_failures=4,
        maximum_attempts=5,
    )


def test_agent_runtime_failure_persists_only_stable_bounded_diagnostics() -> None:
    failure = AgentContextError(
        "CONTEXT_PEDAGOGY_POLICY_REJECTED",
        "sensitive internal message",
        {
            "role": "teaching_agent",
            "event_type": "run_failed",
            "actual": {"prompt": "must never persist"},
            "provider": "must never persist",
            "path": "C:/must/never/persist",
            "field": "x" * 129,
        },
    )

    assert _sanitized_failure(failure, attempt=3) == {
        "code": "WORKFLOW_EXECUTION_FAILED",
        "exception_type": "AgentContextError",
        "attempt": 3,
        "runtime_error": {
            "code": "CONTEXT_PEDAGOGY_POLICY_REJECTED",
            "details": {
                "role": "teaching_agent",
                "event_type": "run_failed",
            },
        },
    }


def test_turn_boundary_failure_persists_only_the_fixed_stage() -> None:
    failure = WorkflowBoundaryError("FINAL_CONTEXT_BUILD")
    failure.__cause__ = ValueError("secret prompt and C:/private/path must never persist")

    assert _sanitized_failure(failure, attempt=2) == {
        "code": "WORKFLOW_EXECUTION_FAILED",
        "exception_type": "WorkflowBoundaryError",
        "attempt": 2,
        "boundary_stage": "FINAL_CONTEXT_BUILD",
    }


def test_runtime_substage_is_preserved_without_source_error_data() -> None:
    source = RuntimeBoundaryError(RuntimeBoundaryStage.CONSTRUCT_AGENT_DECISION)
    source.__cause__ = ValueError("secret prompt at C:/private/student.json")
    failure = _final_runtime_boundary(source)
    failure.__cause__ = source

    sanitized = _sanitized_failure(failure, attempt=4)

    assert sanitized == {
        "code": "WORKFLOW_EXECUTION_FAILED",
        "exception_type": "WorkflowBoundaryError",
        "attempt": 4,
        "boundary_stage": "FINAL_RUNTIME_CONSTRUCT_AGENT_DECISION",
    }
    assert "secret prompt" not in json.dumps(sanitized)
    assert "private/student.json" not in json.dumps(sanitized)


def test_final_decision_substages_are_whitelisted_and_redacted() -> None:
    stages = (
        "FINAL_DECISION_LOAD_RUN",
        "OUTCOME_AUTHORITY",
        "FINAL_DECISION_SHAPE",
        "PROVIDER_RECEIPT_HISTORY",
        "RUNTIME_TRACE_AUTHORITY",
        "PROVIDER_DECISION_WIRE",
        "RECORD_RECEIPT",
    )

    for stage in stages:
        failure = WorkflowBoundaryError(stage)
        failure.__cause__ = ValueError("secret Provider output at C:/private/student.json")
        sanitized = _sanitized_failure(failure, attempt=5)

        assert sanitized == {
            "code": "WORKFLOW_EXECUTION_FAILED",
            "exception_type": "WorkflowBoundaryError",
            "attempt": 5,
            "boundary_stage": stage,
        }
        encoded = json.dumps(sanitized)
        assert "secret Provider output" not in encoded
        assert "private/student.json" not in encoded
