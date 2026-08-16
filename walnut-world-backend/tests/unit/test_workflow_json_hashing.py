"""Workflow JSON digests support finite model numbers and remain fail-closed."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from walnut_backend.adapters.postgres import learner_projection_jobs, run_outcomes
from walnut_backend.adapters.postgres.learner_projection_jobs import (
    LearnerProjectionInvariantError,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    WorkflowInvariantError,
    workflow_json_sha256,
    workflow_receipt_sha256,
)


def _decision_with_fractional_inference() -> dict[str, Any]:
    return {
        "draft": {
            "role": "teaching_agent",
            "response_type": "hint",
            "message": "bounded hint",
            "question": None,
            "hint_level": 1,
            "learner_inference": {
                "concept": "for_loop",
                "score_delta": -0.1,
                "confidence": 0.8,
                "reason": "evidence grounded",
                "evidence_ids": ["evidence_run_0001"],
            },
            "skill_patch": None,
            "requires_student_confirmation": False,
        },
        "message_key": "agent.teaching_agent.hint",
    }


def test_final_decision_receipt_digest_accepts_fractional_inference_and_tamper_changes_hash() -> None:
    receipt = {
        "schema_version": "1.0.0",
        "outcome_event_id": "evt_outcome_0001",
        "outcome_sha256": "a" * 64,
        "run_id": "run_hash_0001",
        "invocation_request_sha256": "b" * 64,
        "provider_result_receipts": [],
        "decision": _decision_with_fractional_inference(),
    }

    digest = workflow_receipt_sha256(receipt)
    assert len(digest) == 64

    changed = copy.deepcopy(receipt)
    changed["decision"]["draft"]["learner_inference"]["confidence"] = 0.7
    assert workflow_receipt_sha256(changed) != digest


def test_learner_objective_and_result_digests_accept_fractional_final_decision() -> None:
    objective = {
        "schema_version": "1.0.0",
        "final_decision": _decision_with_fractional_inference(),
    }
    result = {
        "schema_version": "1.0.0",
        "objective": objective,
        "projection": {"applied": True},
    }

    row = cast(
        Any,
        SimpleNamespace(
            request_sha256=workflow_json_sha256(objective),
            projection_json=objective,
        ),
    )
    learner_projection_jobs._verify_objective_hash(row)  # pyright: ignore[reportPrivateUsage]
    assert len(workflow_json_sha256(result)) == 64

    row.projection_json["final_decision"]["draft"]["learner_inference"][
        "score_delta"
    ] = -0.2
    with pytest.raises(LearnerProjectionInvariantError, match="objective hash is corrupt"):
        learner_projection_jobs._verify_objective_hash(row)  # pyright: ignore[reportPrivateUsage]


def test_all_workflow_hashes_reject_nonfinite_numbers() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(WorkflowInvariantError, match="finite canonical JSON"):
            workflow_json_sha256({"value": value})


def test_receipt_validators_use_the_workflow_hash_boundary() -> None:
    # Guard against future receipt readers drifting back to the integer-only
    # cross-language wire digest. The final-decision validator is the critical
    # path that exposed this mismatch.
    source = run_outcomes.validate_final_decision_receipt.__code__
    assert "workflow_receipt_sha256" in source.co_names
