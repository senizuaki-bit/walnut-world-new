"""Fail-closed policy gates for the INT2 one-file Patch decision."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from walnut_backend.adapters.postgres import run_outcomes
from walnut_backend.adapters.postgres.product_interactions import (
    _interaction_projection_kind,
    _patch_job_receipts_have_authority,
    _patch_provider_decision_draft,
    _validated_entrypoint_operation,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    WorkflowInvariantError,
    workflow_receipt_sha256,
)
from walnut_backend.application.game.skill_builds import InvalidSkillBuildRequest


def _draft() -> dict[str, object]:
    source = "int main() { return 1; }\n"
    return {
        "source_bundle": {
            "language": "CPP20",
            "entrypoint": "main.cpp",
            "files": [
                {
                    "path": "main.cpp",
                    "content": source,
                    "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
                }
            ],
        }
    }


def test_only_exact_one_current_entrypoint_upsert_is_accepted() -> None:
    draft = _draft()
    source = draft["source_bundle"]
    assert isinstance(source, dict)
    files = source["files"]
    assert isinstance(files, list) and isinstance(files[0], dict)
    replacement = "int main() { return 0; }\n"
    operation = {
        "operation": "UPSERT_FILE",
        "path": "main.cpp",
        "previous_content_sha256": files[0]["content_sha256"],
        "content": replacement,
        "content_sha256": hashlib.sha256(replacement.encode()).hexdigest(),
    }

    assert _validated_entrypoint_operation(draft, {"operations": [operation]}) == operation


def test_skill_patch_projection_is_a_distinct_no_run_authority_branch() -> None:
    value = {
        "role": "teaching_agent",
        "response_type": "skill_patch",
        "question": None,
        "hint_level": 4,
        "skill_patch": {"patch_id": "patch_authority"},
        "feedback": {"run_id": None},
    }

    assert _interaction_projection_kind(value) == "SKILL_PATCH_NO_RUN"

    value["feedback"] = {"run_id": "run_fabricated"}
    assert _interaction_projection_kind(value) is None


def _receipt(
    step_name: str,
    *,
    fencing_token: int,
    input_sha256: str = "a" * 64,
    payload: dict[str, object] | None = None,
) -> Any:
    value = payload or {"step": step_name}
    return SimpleNamespace(
        step_name=step_name,
        fencing_token=fencing_token,
        input_sha256=input_sha256,
        output_sha256=workflow_receipt_sha256(value),
        receipt_json=value,
    )


def test_patch_receipt_authority_accepts_closed_lost_response_reconciliation() -> None:
    required = {
        "PATCH_PROVIDER_DISPATCH_01",
        "PATCH_PROVIDER_DISPATCH_02",
        "PATCH_PROVIDER_RESULT_01",
        "PATCH_PROVIDER_RESULT_02",
        "PATCH_PROPOSAL_DERIVED",
        "TURN_COMPLETED",
    }
    receipts = [_receipt(name, fencing_token=3) for name in sorted(required)]
    receipts.extend(
        _receipt(
            f"WORKER_RECONCILE_{token}",
            fencing_token=token,
            payload={
                "code": "WORKFLOW_EXECUTION_FAILED",
                "exception_type": exception_type,
                "attempt": token,
                "retry_after_seconds": 1,
            },
        )
        for token, exception_type in (
            (1, "DurableLlmDispatchUnknown"),
            (2, "DurableLlmReceiptCommitUnknown"),
        )
    )
    job: Any = SimpleNamespace(fencing_token=3, request_sha256="a" * 64)

    assert _patch_job_receipts_have_authority(receipts, job, required)


def test_live_single_provider_unknown_ack_reconciliation_is_closed() -> None:
    required = {
        "PATCH_PROVIDER_DISPATCH_01",
        "PATCH_PROVIDER_RESULT_01",
        "PATCH_PROPOSAL_DERIVED",
        "TURN_COMPLETED",
    }
    receipts = [
        _receipt("PATCH_PROVIDER_DISPATCH_01", fencing_token=1),
        _receipt("PATCH_PROVIDER_RESULT_01", fencing_token=2),
        _receipt("PATCH_PROPOSAL_DERIVED", fencing_token=2),
        _receipt("TURN_COMPLETED", fencing_token=2),
        _receipt(
            "WORKER_RECONCILE_1",
            fencing_token=1,
            payload={
                "code": "WORKFLOW_EXECUTION_FAILED",
                "exception_type": "DurableLlmDispatchUnknown",
                "attempt": 1,
                "retry_after_seconds": 1,
            },
        ),
    ]
    job: Any = SimpleNamespace(fencing_token=2, request_sha256="a" * 64)

    assert _patch_job_receipts_have_authority(receipts, job, required)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_extra",
        "suffix_fence_mismatch",
        "input_mismatch",
        "output_mismatch",
        "payload_mismatch",
        "base_exception_type",
    ],
)
def test_patch_reconciliation_receipt_corruption_fails_closed(mutation: str) -> None:
    required = {
        "PATCH_PROVIDER_DISPATCH_01",
        "PATCH_PROVIDER_RESULT_01",
        "PATCH_PROPOSAL_DERIVED",
        "TURN_COMPLETED",
    }
    receipts = [_receipt(name, fencing_token=2) for name in sorted(required)]
    reconcile = _receipt(
        "WORKER_RECONCILE_1",
        fencing_token=1,
        payload={
            "code": "WORKFLOW_EXECUTION_FAILED",
            "exception_type": "DurableLlmDispatchPending",
            "attempt": 1,
        },
    )
    receipts.append(reconcile)
    job: Any = SimpleNamespace(fencing_token=2, request_sha256="a" * 64)
    corrupted = deepcopy(receipts)
    if mutation == "unknown_extra":
        corrupted[-1].step_name = "WORKER_FAILURE_1"
    elif mutation == "suffix_fence_mismatch":
        corrupted[-1].fencing_token = 2
    elif mutation == "input_mismatch":
        corrupted[-1].input_sha256 = "b" * 64
    elif mutation == "output_mismatch":
        corrupted[-1].output_sha256 = "c" * 64
    elif mutation == "payload_mismatch":
        corrupted[-1].receipt_json["attempt"] = 2
        corrupted[-1].output_sha256 = workflow_receipt_sha256(
            corrupted[-1].receipt_json
        )
    elif mutation == "base_exception_type":
        corrupted[-1].receipt_json["exception_type"] = (
            "WorkflowReconciliationPending"
        )
        corrupted[-1].output_sha256 = workflow_receipt_sha256(
            corrupted[-1].receipt_json
        )
    else:
        raise AssertionError(mutation)

    assert not _patch_job_receipts_have_authority(corrupted, job, required)


_PATCH_PROVIDER_DRAFT = {
    "role": "teaching_agent",
    "response_type": "skill_patch",
    "message": "Review this exact replacement before deciding.",
    "question": None,
    "hint_level": 4,
    "learner_inference": None,
    "skill_patch": {
        "replacement_content": "int main() { return 0; }\n",
        "rationale": "Replace the exact failed entrypoint.",
    },
    "requires_student_confirmation": True,
}


def test_patch_provider_draft_rebuild_matches_single_terminal_result() -> None:
    proposal: Any = SimpleNamespace(
        agent_proposal_json={
            "operation": {
                "content": _PATCH_PROVIDER_DRAFT["skill_patch"][
                    "replacement_content"
                ]
            },
            "rationale": _PATCH_PROVIDER_DRAFT["skill_patch"]["rationale"],
        }
    )
    interaction = {
        "role": "teaching_agent",
        "response_type": "skill_patch",
        "question": None,
        "hint_level": 4,
        "feedback": {"message": _PATCH_PROVIDER_DRAFT["message"]},
        "skill_patch": {"requires_student_confirmation": True},
    }

    rebuilt = _patch_provider_decision_draft(interaction, proposal)

    assert rebuilt == _PATCH_PROVIDER_DRAFT
    run_outcomes.validate_provider_decision_wire(
        (_provider_result_receipt(),),
        decision_draft=cast(dict[str, Any], rebuilt),
        evidence_refs=(),
    )


def _provider_result_receipt(
    *,
    failure: bool = False,
    repairable: bool = True,
    state: str = "SUCCEEDED",
) -> Any:
    result: dict[str, object]
    if failure:
        result = {
            "schema_version": "1.0.0",
            "outcome": "FAILURE",
            "error": {
                "code": "INVARIANT_VIOLATION",
                "category": "INVARIANT",
                "retryable": False,
                "user_message_key": "system.invariant_violation",
                "stage": "MODEL_OUTPUT",
                "message": None,
                "details": {"repairable": repairable},
                "evidence_ids": [],
            },
        }
    else:
        result = {
            "schema_version": "1.0.0",
            "outcome": "SUCCESS",
            "reply": {
                "output": {
                    "kind": "decision",
                    "decision": deepcopy(_PATCH_PROVIDER_DRAFT),
                    "tool_calls": [],
                },
                "provider": "fake-provider",
                "model": "fake-model-v1",
                "source": "provider",
                "degraded": False,
                "fallback_reason": None,
                "input_tokens": 2,
                "output_tokens": 3,
                "evidence_refs": [],
            },
        }
    return SimpleNamespace(
        receipt_json={
            "schema_version": "2.0.0",
            "dispatch": {
                "dispatch_id": "llm_dispatch_00000000000000000001",
                "request_sha256": "a" * 64,
                "context_sha256": "b" * 64,
                "provider": "fake-provider",
                "model": "fake-model-v1",
                "completion_sha256": "c" * 64,
                "state": state,
                "generation_count": 1 if state == "SUCCEEDED" else 0,
                "raw_response_sha256": "d" * 64 if state == "SUCCEEDED" else None,
            },
            "result": result,
        }
    )


def test_patch_provider_history_accepts_one_closed_schema_repair() -> None:
    rows = [
        SimpleNamespace(step_name=f"PATCH_PROVIDER_{kind}_{ordinal:02d}")
        for ordinal in (1, 2)
        for kind in ("DISPATCH", "RESULT")
    ]

    results, _ = run_outcomes._bounded_provider_receipt_rows(  # pyright: ignore[reportPrivateUsage]
        cast(Any, rows),
        namespace="PATCH",
        max_results=2,
        authority_label="Patch",
    )

    assert [item.step_name for item in results] == [
        "PATCH_PROVIDER_RESULT_01",
        "PATCH_PROVIDER_RESULT_02",
    ]
    run_outcomes.validate_provider_decision_wire(
        (_provider_result_receipt(failure=True), _provider_result_receipt()),
        decision_draft=_PATCH_PROVIDER_DRAFT,
        evidence_refs=(),
    )


@pytest.mark.parametrize(
    "step_names",
    [
        [
            "PATCH_PROVIDER_DISPATCH_01",
            "PATCH_PROVIDER_RESULT_01",
            "PATCH_PROVIDER_DISPATCH_03",
            "PATCH_PROVIDER_RESULT_03",
        ],
        [
            "PATCH_PROVIDER_DISPATCH_01",
            "PATCH_PROVIDER_RESULT_01",
            "PATCH_PROVIDER_RESULT_02",
        ],
        [
            "PATCH_PROVIDER_DISPATCH_01",
            "PATCH_PROVIDER_RESULT_01",
            "PATCH_PROVIDER_UNKNOWN_02",
        ],
    ],
    ids=["gap", "unpaired", "unknown"],
)
def test_patch_provider_history_name_corruption_fails_closed(
    step_names: list[str],
) -> None:
    with pytest.raises(WorkflowInvariantError):
        run_outcomes._bounded_provider_receipt_rows(  # pyright: ignore[reportPrivateUsage]
            cast(Any, [SimpleNamespace(step_name=name) for name in step_names]),
            namespace="PATCH",
            max_results=2,
            authority_label="Patch",
        )


@pytest.mark.parametrize(
    ("receipts"),
    [
        (_provider_result_receipt(failure=True, repairable=False), _provider_result_receipt()),
        (_provider_result_receipt(failure=True, state="FAILED"), _provider_result_receipt()),
        (_provider_result_receipt(failure=True),),
    ],
    ids=["nonrepairable", "failed-resource", "terminal-failure"],
)
def test_patch_provider_failure_history_fails_closed(receipts: tuple[Any, ...]) -> None:
    with pytest.raises(WorkflowInvariantError):
        run_outcomes.validate_provider_decision_wire(
            receipts,
            decision_draft=_PATCH_PROVIDER_DRAFT,
            evidence_refs=(),
        )


def test_patch_provider_terminal_decision_must_match_derived_draft() -> None:
    receipt = _provider_result_receipt()
    decision = receipt.receipt_json["result"]["reply"]["output"]["decision"]
    decision["skill_patch"]["replacement_content"] = "tampered\n"

    with pytest.raises(WorkflowInvariantError, match="Provider authority"):
        run_outcomes.validate_provider_decision_wire(
            (receipt,),
            decision_draft=_PATCH_PROVIDER_DRAFT,
            evidence_refs=(),
        )


@pytest.mark.parametrize(
    "operations",
    [
        [],
        [{"operation": "DELETE_FILE", "path": "main.cpp", "previous_content_sha256": "0" * 64}],
        [{"operation": "SET_DISPLAY_NAME", "display_name": "hidden mutation"}],
        [
            {"operation": "UPSERT_FILE", "path": "main.cpp"},
            {"operation": "UPSERT_FILE", "path": "main.cpp"},
        ],
        [{"operation": "UPSERT_FILE", "path": "other.cpp"}],
    ],
)
def test_multi_file_or_non_entrypoint_patch_fails_closed(
    operations: list[dict[str, object]],
) -> None:
    with pytest.raises(InvalidSkillBuildRequest):
        _validated_entrypoint_operation(_draft(), {"operations": operations})
