"""Public Evidence bytes close their terminal payload hash and durable source."""

from __future__ import annotations

import copy
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    ContractError,
    ErrorCategory,
    RequestContext,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres import run_evidence
from walnut_backend.adapters.postgres.models import (
    EvidenceRow,
    error_data,
    request_context_data,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    WorkflowInvariantError,
    workflow_receipt_sha256,
)


def test_evidence_payload_hash_and_source_are_exact() -> None:
    row = _evidence_row()
    run_evidence._validate_evidence_document(row)  # pyright: ignore[reportPrivateUsage]

    changed = copy.deepcopy(row.evidence_json)
    changed["payload"]["outcome"] = "CORRUPT"
    row.evidence_json = changed
    with pytest.raises(WorkflowInvariantError, match="terminal/hash/source"):
        run_evidence._validate_evidence_document(row)  # pyright: ignore[reportPrivateUsage]


def test_public_read_path_reuses_run_outcome_and_terminal_validators() -> None:
    source = inspect.getsource(
        run_evidence._validated_run_for_public_read  # pyright: ignore[reportPrivateUsage]
    )
    assert "load_validated_run(" in source
    assert "workflow_step_receipt_id(" in source
    assert "run_authority_sha256(" in source
    assert "validate_canonical_outcome_event(" in source
    assert "validate_terminal_projection(" in source
    assert source.count("validation_state=validation_state") == 3
    assert "TerminalProjectionValidationState()" in source
    assert "context.content_ref" not in source

    failed_terminal = inspect.getsource(
        run_evidence._validate_failed_terminal_outcome  # pyright: ignore[reportPrivateUsage]
    )
    assert "canonical_outcome_occurred_at(" in failed_terminal

    run_lookup = inspect.getsource(run_evidence.PostgresRunEvidenceStore.get_run)
    evidence_lookup = inspect.getsource(run_evidence.PostgresRunEvidenceStore.get_evidence)
    assert "validation_state=validation_state" in run_lookup
    assert evidence_lookup.count("validation_state=validation_state") == 2
    assert "RunRow.content_hash == context.content_ref" not in run_lookup
    assert "EvidenceRow.content_hash == context.content_ref" not in evidence_lookup


def test_failed_final_provider_receipt_is_closed_and_tamper_fails() -> None:
    error = ContractError(
        code="DEPENDENCY_UNAVAILABLE",
        category=ErrorCategory.DEPENDENCY,
        retryable=True,
        user_message_key="dependency.temporarily_unavailable",
        stage="PROVIDER",
        message="provider unavailable",
    )
    dispatch = {
        "dispatch_id": f"llmdsp_{'1' * 40}",
        "request_sha256": "a" * 64,
        "context_sha256": "b" * 64,
        "provider": "provider-unit",
        "model": "model-unit",
        "completion_sha256": "c" * 64,
        "state": "FAILED",
        "generation_count": 1,
        "raw_response_sha256": None,
    }
    envelope = {
        "schema_version": "2.0.0",
        "dispatch": dispatch,
        "result": {
            "schema_version": "1.0.0",
            "outcome": "FAILURE",
            "error": error_data(error),
        },
    }
    receipt = cast(
        Any,
        SimpleNamespace(
            input_sha256="a" * 64,
            output_sha256=workflow_receipt_sha256(envelope),
            receipt_json=envelope,
        ),
    )
    run_evidence._validate_failed_provider_receipts(  # pyright: ignore[reportPrivateUsage]
        (receipt,)
    )
    changed = copy.deepcopy(envelope)
    changed["dispatch"]["state"] = "SUCCEEDED"
    receipt.receipt_json = changed
    receipt.output_sha256 = workflow_receipt_sha256(changed)
    with pytest.raises(WorkflowInvariantError, match="state|raw_response_sha256"):
        run_evidence._validate_failed_provider_receipts(  # pyright: ignore[reportPrivateUsage]
            (receipt,)
        )


def _evidence_row() -> EvidenceRow:
    now = datetime(2026, 8, 12, 2, 3, 4, tzinfo=UTC)
    context = RequestContext(
        request_id="req_evidence_unit_01",
        correlation_id="corr_evidence_unit_01",
        trace_id="trace_evidence_unit_01",
        requested_at=now,
        actor=ActorRef(
            "tenant_evidence_unit",
            "student_evidence_unit",
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("UNIT_EVIDENCE", "1.0.0", "a" * 64),
    )
    payload = {
        "evidence_kind": "BUILD_CERTIFICATION",
        "build_id": "build_evidence_unit_01",
        "outcome": "CERTIFIED",
    }
    digest = canonical_json_sha256(payload)
    timestamp = now.isoformat().replace("+00:00", "Z")
    value = {
        "request_context": request_context_data(context),
        "evidence_ref": {
            "evidence_id": "evidence_unit_01",
            "evidence_type": "TEST_REPORT",
            "created_at": timestamp,
            "sha256": digest,
            "uri": "/v1/evidence/evidence_unit_01",
        },
        "subject": {"learner_id": "student_evidence_unit"},
        "source": {
            "source_type": "SKILL_BUILD",
            "source_id": "build_evidence_unit_01",
            "command_id": "cmd_evidence_unit_01",
            "world_id": "world_evidence_unit_01",
        },
        "occurred_at": timestamp,
        "recorded_at": timestamp,
        "integrity": {
            "payload_sha256": digest,
            "previous_evidence_sha256": None,
        },
        "payload": payload,
        "related_evidence": [],
        "versions": {"api_version": "1.0.0"},
    }
    return EvidenceRow(
        evidence_id="evidence_unit_01",
        tenant_id="tenant_evidence_unit",
        actor_id="student_evidence_unit",
        content_hash="a" * 64,
        command_id="cmd_evidence_unit_01",
        recorded_at=now,
        evidence_json=value,
    )
