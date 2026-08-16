"""Final Provider request history distinguishes repair attempts from tool execution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from walnut_backend.adapters.postgres import run_outcomes, workflow_jobs
from walnut_backend.adapters.postgres.agent_runtime import _agent_trace_audit_id

_DRAFT = {
    "role": "book_agent",
    "response_type": "growth_summary",
    "message": "The exact Skill Run completed.",
    "question": None,
    "hint_level": None,
    "learner_inference": None,
    "skill_patch": None,
    "requires_student_confirmation": False,
}
_RAW_CALL = {
    "call_id": "call_valid_0001",
    "name": "get_current_run",
    "arguments": {},
}
_DURABLE_CALL = {
    "execution_id": "toolexec_000000000000000000000000",
    "model_call_id": "call_valid_0001",
    "name": "get_current_run",
    "arguments": {},
    "result_summary": {},
}


def test_invalid_tool_calls_receipt_is_one_repair_not_a_successful_tool_round() -> None:
    history = run_outcomes._classify_final_provider_request_history(  # pyright: ignore[reportPrivateUsage]
        _history_items("tool_calls", "decision"),
        [],
    )

    assert history.successful_tool_rounds == 0
    assert history.invalid_attempts == 1
    run_outcomes.validate_provider_decision_wire(
        cast(
            Any,
            (
                _receipt(
                    kind="tool_calls",
                    tool_calls=[
                        {
                            "call_id": "call_rejected_0001",
                            "name": "forbidden_tool",
                            "arguments": {},
                        }
                    ],
                ),
                _receipt(kind="decision"),
            ),
        ),
        decision_draft=_DRAFT,
        evidence_refs=(),
        decision=_decision(tool_calls=[], receipt_count=2),
    )


def test_runtime_trace_accepts_invalid_tool_shape_then_terminal_decision() -> None:
    receipts = (
        _receipt(kind="tool_calls", tool_calls=[]),
        _receipt(kind="decision"),
    )
    role = "book_agent"
    trace_id = "trace_final_provider_history"
    rows = _trace_rows(
        [
            _trace(name, role=role, trace_id=trace_id)
            for name in (
                "agent.turn.started",
                "agent.model.requested",
                "agent.output.invalid",
                "agent.model.requested",
                "agent.turn.finished",
            )
        ]
    )
    authority = SimpleNamespace(
        job=SimpleNamespace(tenant_id="tenant_history", command_id="cmd_history"),
        turn=SimpleNamespace(turn_id="turn_history"),
        context=SimpleNamespace(trace_id=trace_id),
    )

    asyncio.run(
        run_outcomes.validate_agent_decision_runtime_authority(
            cast(Any, _TraceSession(rows)),
            authority=cast(Any, authority),
            receipts=cast(Any, receipts),
            decision={
                "draft": {"role": role},
                "runtime_warnings": [],
                "tool_calls": [],
            },
        )
    )


def test_durable_tool_records_prove_exactly_one_successful_tool_round() -> None:
    history = run_outcomes._classify_final_provider_request_history(  # pyright: ignore[reportPrivateUsage]
        _history_items("tool_calls", "decision"),
        [_DURABLE_CALL],
    )

    assert history.successful_tool_rounds == 1
    assert history.invalid_attempts == 0
    run_outcomes.validate_provider_decision_wire(
        cast(
            Any,
            (
                _receipt(kind="tool_calls", tool_calls=[_RAW_CALL]),
                _receipt(kind="decision"),
            ),
        ),
        decision_draft=_DRAFT,
        evidence_refs=(),
        decision=_decision(tool_calls=[_DURABLE_CALL], receipt_count=2),
    )


def test_runtime_trace_accepts_one_durable_tool_round_then_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = "book_agent"
    trace_id = "trace_final_provider_tool_history"
    execution_id = run_outcomes._tool_execution_id(  # pyright: ignore[reportPrivateUsage]
        "cmd_history",
        "turn_history",
        1,
        "get_current_run",
    )
    tool = {**_DURABLE_CALL, "execution_id": execution_id}
    traces = [
        _trace("agent.turn.started", role=role, trace_id=trace_id),
        _trace("agent.model.requested", role=role, trace_id=trace_id),
        _trace(
            "agent.tool.started",
            role=role,
            trace_id=trace_id,
            fields={
                "execution_id": execution_id,
                "tool": "get_current_run",
                "ordinal": 1,
            },
        ),
        _trace(
            "agent.tool.succeeded",
            role=role,
            trace_id=trace_id,
            fields={
                "execution_id": execution_id,
                "tool": "get_current_run",
                "evidence_count": 0,
            },
        ),
        _trace("agent.model.requested", role=role, trace_id=trace_id),
        _trace("agent.turn.finished", role=role, trace_id=trace_id),
    ]
    authority = SimpleNamespace(
        job=SimpleNamespace(tenant_id="tenant_history", command_id="cmd_history"),
        turn=SimpleNamespace(turn_id="turn_history"),
        context=SimpleNamespace(trace_id=trace_id),
    )

    async def validate_tool_summary(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(run_outcomes, "_validate_tool_summary", validate_tool_summary)
    asyncio.run(
        run_outcomes.validate_agent_decision_runtime_authority(
            cast(Any, _TraceSession(_trace_rows(traces))),
            authority=cast(Any, authority),
            receipts=cast(
                Any,
                (
                    _receipt(kind="tool_calls", tool_calls=[_RAW_CALL]),
                    _receipt(kind="decision"),
                ),
            ),
            decision={
                "draft": {"role": role},
                "runtime_warnings": [],
                "tool_calls": [tool],
            },
        )
    )


@pytest.mark.parametrize(
    ("output_kinds", "durable_tool_calls"),
    (
        ((), []),
        (("tool_calls",), []),
        (("failure",), []),
        (("tool_calls", "failure"), [_DURABLE_CALL]),
        (("unknown", "decision"), []),
        (("decision",), [_DURABLE_CALL]),
        (("decision", "decision"), [_DURABLE_CALL]),
        (("decision", "decision", "decision"), []),
    ),
)
def test_impossible_provider_request_histories_fail_closed(
    output_kinds: tuple[str, ...],
    durable_tool_calls: list[dict[str, object]],
) -> None:
    with pytest.raises(
        workflow_jobs.WorkflowInvariantError, match="impossible Provider request history"
    ):
        run_outcomes._classify_final_provider_request_history(  # pyright: ignore[reportPrivateUsage]
            _history_items(*output_kinds),
            durable_tool_calls,
        )


def test_repeated_tool_batch_after_success_is_one_invalid_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = (
        _receipt(kind="tool_calls", tool_calls=[_RAW_CALL]),
        _receipt(kind="tool_calls", tool_calls=[_RAW_CALL]),
        _receipt(kind="decision"),
    )

    history = run_outcomes._classify_final_provider_request_history(  # pyright: ignore[reportPrivateUsage]
        _history_items("tool_calls", "tool_calls", "decision"),
        [_DURABLE_CALL],
    )
    assert history.successful_tool_rounds == 1
    assert history.invalid_attempts == 1
    run_outcomes.validate_provider_decision_wire(
        cast(Any, receipts),
        decision_draft=_DRAFT,
        evidence_refs=(),
        decision=_decision(tool_calls=[_DURABLE_CALL], receipt_count=3),
    )
    _assert_tool_runtime_trace_accepts(
        receipts=receipts,
        invalid_attempts=1,
        monkeypatch=monkeypatch,
    )


def test_invalid_tool_batch_before_valid_tool_round_is_one_repair() -> None:
    rejected_call = {**_RAW_CALL, "name": "forbidden_tool"}
    receipts = (
        _receipt(kind="tool_calls", tool_calls=[rejected_call]),
        _receipt(kind="tool_calls", tool_calls=[_RAW_CALL]),
        _receipt(kind="decision"),
    )

    history = run_outcomes._classify_final_provider_request_history(  # pyright: ignore[reportPrivateUsage]
        _history_items("tool_calls", "tool_calls", "decision"),
        [_DURABLE_CALL],
    )
    assert history.successful_tool_rounds == 1
    assert history.invalid_attempts == 1
    run_outcomes.validate_provider_decision_wire(
        cast(Any, receipts),
        decision_draft=_DRAFT,
        evidence_refs=(),
        decision=_decision(tool_calls=[_DURABLE_CALL], receipt_count=3),
    )


@pytest.mark.parametrize(
    "order",
    (
        "failure_decision",
        "failure_tool_decision",
        "tool_failure_decision",
    ),
)
def test_repairable_provider_failure_is_one_invalid_attempt(
    order: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = {
        "failure_decision": (_failure_receipt(), _receipt(kind="decision")),
        "failure_tool_decision": (
            _failure_receipt(),
            _receipt(kind="tool_calls", tool_calls=[_RAW_CALL]),
            _receipt(kind="decision"),
        ),
        "tool_failure_decision": (
            _receipt(kind="tool_calls", tool_calls=[_RAW_CALL]),
            _failure_receipt(),
            _receipt(kind="decision"),
        ),
    }[order]
    tools = [_DURABLE_CALL] if len(receipts) == 3 else []
    parsed = tuple(
        run_outcomes._parse_final_provider_receipt(cast(Any, receipt))  # pyright: ignore[reportPrivateUsage]
        for receipt in receipts
    )
    history = run_outcomes._classify_final_provider_request_history(  # pyright: ignore[reportPrivateUsage]
        parsed,
        tools,
    )
    assert history.successful_tool_rounds == int(bool(tools))
    assert history.invalid_attempts == 1

    run_outcomes.validate_provider_decision_wire(
        cast(Any, receipts),
        decision_draft=_DRAFT,
        evidence_refs=(),
        decision=_decision(
            tool_calls=tools,
            receipt_count=len(receipts),
            failure_count=1,
        ),
    )
    if tools:
        _assert_tool_runtime_trace_accepts(
            receipts=receipts,
            invalid_attempts=1,
            monkeypatch=monkeypatch,
        )
    else:
        _assert_no_tool_runtime_trace_accepts(
            receipts=receipts,
            invalid_attempts=1,
        )


@pytest.mark.parametrize(
    ("repairable", "state", "error_patch", "result_patch"),
    (
        (False, "SUCCEEDED", None, None),
        (True, "FAILED", None, None),
        (True, "SUCCEEDED", {"stage": "PROVIDER"}, None),
        (True, "SUCCEEDED", {"unexpected": True}, None),
        (True, "SUCCEEDED", {"details": {"repairable": "yes"}}, None),
        (True, "SUCCEEDED", None, {"outcome": "UNKNOWN"}),
        (True, "SUCCEEDED", None, {"reply": {}}),
    ),
)
def test_noncanonical_or_nonrepairable_provider_failure_fails_closed(
    repairable: bool,
    state: str,
    error_patch: dict[str, object] | None,
    result_patch: dict[str, object] | None,
) -> None:
    failure = _failure_receipt(
        repairable=repairable,
        state=state,
        error_patch=error_patch,
        result_patch=result_patch,
    )
    with pytest.raises(workflow_jobs.WorkflowInvariantError):
        run_outcomes.validate_provider_decision_wire(
            cast(Any, (failure, _receipt(kind="decision"))),
            decision_draft=_DRAFT,
            evidence_refs=(),
            decision=_decision(tool_calls=[], receipt_count=2, failure_count=1),
        )


def test_tool_receipt_tamper_cannot_bind_to_durable_tool_record() -> None:
    changed_call = {**_RAW_CALL, "arguments": {"tampered": True}}

    with pytest.raises(workflow_jobs.WorkflowInvariantError, match="Provider authority"):
        run_outcomes.validate_provider_decision_wire(
            cast(
                Any,
                (
                    _receipt(kind="tool_calls", tool_calls=[changed_call]),
                    _receipt(kind="decision"),
                ),
            ),
            decision_draft=_DRAFT,
            evidence_refs=(),
            decision=_decision(tool_calls=[_DURABLE_CALL], receipt_count=2),
        )


def test_provider_tool_model_call_id_must_be_nonempty() -> None:
    raw_call = {**_RAW_CALL, "call_id": ""}
    durable_call = {**_DURABLE_CALL, "model_call_id": ""}

    with pytest.raises(workflow_jobs.WorkflowInvariantError):
        run_outcomes.validate_provider_decision_wire(
            cast(
                Any,
                (
                    _receipt(kind="tool_calls", tool_calls=[raw_call]),
                    _receipt(kind="decision"),
                ),
            ),
            decision_draft=_DRAFT,
            evidence_refs=(),
            decision=_decision(tool_calls=[durable_call], receipt_count=2),
        )


@pytest.mark.parametrize(
    "case",
    (
        "audit_id",
        "outcome",
        "duplicate_request",
        "out_of_range_request",
        "repair_attempt",
        "unknown_trace",
        "phantom_tool",
    ),
)
def test_runtime_trace_identity_corruption_fails_closed(case: str) -> None:
    role = "book_agent"
    trace_id = "trace_final_provider_identity"
    records = [
        _trace("agent.turn.started", role=role, trace_id=trace_id),
        _trace(
            "agent.model.requested",
            role=role,
            trace_id=trace_id,
            fields={"request_number": 1},
        ),
        _trace(
            "agent.output.invalid",
            role=role,
            trace_id=trace_id,
            fields={"repair_attempt": 1},
        ),
        _trace(
            "agent.model.requested",
            role=role,
            trace_id=trace_id,
            fields={"request_number": 2},
        ),
        _trace("agent.turn.finished", role=role, trace_id=trace_id),
    ]
    rows = [_trace_row(record) for record in records]
    if case == "audit_id":
        rows[0].audit_id = "audit_forged"
    elif case == "outcome":
        rows[0].outcome = "FAILURE"
    elif case == "duplicate_request":
        rows[3] = _trace_row(records[1])
    elif case == "out_of_range_request":
        rows[3] = _trace_row(
            _trace(
                "agent.model.requested",
                role=role,
                trace_id=trace_id,
                fields={"request_number": 999},
            )
        )
    elif case == "repair_attempt":
        rows[2] = _trace_row(
            _trace(
                "agent.output.invalid",
                role=role,
                trace_id=trace_id,
                fields={"repair_attempt": 7},
            )
        )
    elif case == "unknown_trace":
        rows.append(
            SimpleNamespace(
                audit_id="audit_unknown",
                tenant_id="tenant_history",
                operation="AGENT_RUNTIME_TRACE",
                outcome="SUCCESS",
                record_json=_trace("agent.turn.ghost", role=role, trace_id=trace_id),
            )
        )
    else:
        rows.append(
            _trace_row(
                _trace(
                    "agent.tool.started",
                    role=role,
                    trace_id=trace_id,
                    fields={"execution_id": "phantom", "tool": "ghost", "ordinal": 99},
                )
            )
        )

    authority = SimpleNamespace(
        job=SimpleNamespace(tenant_id="tenant_history", command_id="cmd_history"),
        turn=SimpleNamespace(turn_id="turn_history"),
        context=SimpleNamespace(trace_id=trace_id),
    )
    receipts = (
        _receipt(kind="tool_calls", tool_calls=[]),
        _receipt(kind="decision"),
    )
    with pytest.raises(workflow_jobs.WorkflowInvariantError):
        asyncio.run(
            run_outcomes.validate_agent_decision_runtime_authority(
                cast(Any, _TraceSession(rows)),
                authority=cast(Any, authority),
                receipts=cast(Any, receipts),
                decision={
                    "draft": {"role": role},
                    "runtime_warnings": [],
                    "tool_calls": [],
                },
            )
        )


def test_trace_write_warning_cannot_hide_scope_tampered_expected_row() -> None:
    role = "book_agent"
    trace_id = "trace_final_provider_scope_tamper"
    record = _trace("agent.turn.started", role=role, trace_id=trace_id)
    row = _trace_row(record)
    row.record_json = {**record, "role": "teaching_agent"}
    authority = SimpleNamespace(
        job=SimpleNamespace(tenant_id="tenant_history", command_id="cmd_history"),
        turn=SimpleNamespace(turn_id="turn_history"),
        context=SimpleNamespace(trace_id=trace_id),
    )

    with pytest.raises(
        workflow_jobs.WorkflowInvariantError,
        match="final Agent trace record drifted",
    ):
        asyncio.run(
            run_outcomes.validate_agent_decision_runtime_authority(
                cast(Any, _ScopeFilteringTraceSession([row])),
                authority=cast(Any, authority),
                receipts=cast(Any, (_receipt(kind="decision"),)),
                decision={
                    "draft": {"role": role},
                    "runtime_warnings": ["TRACE_WRITE_FAILED"],
                    "tool_calls": [],
                },
            )
        )


def _decision(
    *,
    tool_calls: list[dict[str, object]],
    receipt_count: int,
    failure_count: int = 0,
) -> dict[str, object]:
    successful_receipt_count = receipt_count - failure_count
    return {
        "provider": "fake-provider",
        "model": "fake-model-v1",
        "input_tokens": successful_receipt_count * 2,
        "output_tokens": successful_receipt_count * 3,
        "tool_calls": tool_calls,
    }


def _receipt(
    *,
    kind: str,
    tool_calls: list[dict[str, object]] | None = None,
) -> SimpleNamespace:
    output = (
        {"kind": "decision", "decision": dict(_DRAFT), "tool_calls": []}
        if kind == "decision"
        else {"kind": kind, "decision": None, "tool_calls": tool_calls or []}
    )
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
                "state": "SUCCEEDED",
                "generation_count": 1,
                "raw_response_sha256": "d" * 64,
            },
            "result": {
                "schema_version": "1.0.0",
                "outcome": "SUCCESS",
                "reply": {
                    "output": output,
                    "provider": "fake-provider",
                    "model": "fake-model-v1",
                    "source": "provider",
                    "degraded": False,
                    "fallback_reason": None,
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "evidence_refs": [],
                },
            },
        }
    )


def _failure_receipt(
    *,
    repairable: bool = True,
    state: str = "SUCCEEDED",
    error_patch: dict[str, object] | None = None,
    result_patch: dict[str, object] | None = None,
) -> SimpleNamespace:
    error = {
        "code": "INVARIANT_VIOLATION",
        "category": "INVARIANT",
        "retryable": False,
        "user_message_key": "system.invariant_violation",
        "stage": "MODEL_OUTPUT",
        "message": None,
        "details": {"repairable": repairable},
        "evidence_ids": [],
        **(error_patch or {}),
    }
    result = {
        "schema_version": "1.0.0",
        "outcome": "FAILURE",
        "error": error,
        **(result_patch or {}),
    }
    receipt = _receipt(kind="decision")
    receipt.receipt_json["dispatch"]["state"] = state
    if state == "FAILED":
        receipt.receipt_json["dispatch"]["generation_count"] = 0
        receipt.receipt_json["dispatch"]["raw_response_sha256"] = None
    receipt.receipt_json["result"] = result
    return receipt


def _trace(
    name: str,
    *,
    role: str,
    trace_id: str,
    fields: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "turn_id": "turn_history",
        "role": role,
        "fields": fields or {},
        "command_id": "cmd_history",
        "trace_id": trace_id,
    }


def _trace_row(record_json: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        audit_id=_agent_trace_audit_id("tenant_history", record_json),
        tenant_id="tenant_history",
        operation="AGENT_RUNTIME_TRACE",
        outcome="SUCCESS",
        record_json=record_json,
    )


def _trace_rows(records: list[dict[str, object]]) -> list[SimpleNamespace]:
    request_number = 0
    repair_attempt = 0
    rows: list[SimpleNamespace] = []
    for raw in records:
        record = {**raw, "fields": dict(cast(dict[str, object], raw["fields"]))}
        fields = cast(dict[str, object], record["fields"])
        if record["name"] == "agent.model.requested":
            request_number += 1
            fields.setdefault("request_number", request_number)
        elif record["name"] == "agent.output.invalid":
            repair_attempt += 1
            fields.setdefault("repair_attempt", repair_attempt)
        rows.append(_trace_row(record))
    return rows


def _history_items(
    *kinds: str,
) -> tuple[run_outcomes._FinalProviderReceiptItem, ...]:  # pyright: ignore[reportPrivateUsage]
    return tuple(
        run_outcomes._FinalProviderReceiptItem(  # pyright: ignore[reportPrivateUsage]
            kind=kind,
            provider="fake-provider",
            model="fake-model-v1",
            input_tokens=0,
            output_tokens=0,
        )
        for kind in kinds
    )


class _TraceRows:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _TraceSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def scalars(self, statement: object) -> _TraceRows:
        del statement
        return _TraceRows(self._rows)


class _ScopeFilteringTraceSession:
    """Apply the query's expected-ID/current-scope union to in-memory rows."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def scalars(self, statement: object) -> _TraceRows:
        params = cast(Any, statement).compile().params
        expected_ids = {
            item
            for value in params.values()
            if isinstance(value, list | tuple)
            for item in value
            if isinstance(item, str)
        }
        rows = [
            row
            for row in self._rows
            if row.audit_id in expected_ids
            or (
                row.record_json.get("command_id") == "cmd_history"
                and row.record_json.get("turn_id") == "turn_history"
                and row.record_json.get("role") == "book_agent"
            )
        ]
        return _TraceRows(rows)


def _assert_no_tool_runtime_trace_accepts(
    *,
    receipts: tuple[SimpleNamespace, ...],
    invalid_attempts: int,
) -> None:
    role = "book_agent"
    trace_id = "trace_final_provider_failure_history"
    traces = [
        _trace("agent.turn.started", role=role, trace_id=trace_id),
        *[_trace("agent.model.requested", role=role, trace_id=trace_id) for _ in receipts],
        *[
            _trace("agent.output.invalid", role=role, trace_id=trace_id)
            for _ in range(invalid_attempts)
        ],
        _trace("agent.turn.finished", role=role, trace_id=trace_id),
    ]
    authority = SimpleNamespace(
        job=SimpleNamespace(tenant_id="tenant_history", command_id="cmd_history"),
        turn=SimpleNamespace(turn_id="turn_history"),
        context=SimpleNamespace(trace_id=trace_id),
    )
    asyncio.run(
        run_outcomes.validate_agent_decision_runtime_authority(
            cast(Any, _TraceSession(_trace_rows(traces))),
            authority=cast(Any, authority),
            receipts=cast(Any, receipts),
            decision={
                "draft": {"role": role},
                "runtime_warnings": [],
                "tool_calls": [],
            },
        )
    )


def _assert_tool_runtime_trace_accepts(
    *,
    receipts: tuple[SimpleNamespace, ...],
    invalid_attempts: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = "book_agent"
    trace_id = "trace_final_provider_repair_history"
    execution_id = run_outcomes._tool_execution_id(  # pyright: ignore[reportPrivateUsage]
        "cmd_history",
        "turn_history",
        1,
        "get_current_run",
    )
    traces = [
        _trace("agent.turn.started", role=role, trace_id=trace_id),
        *[_trace("agent.model.requested", role=role, trace_id=trace_id) for _ in receipts],
        *[
            _trace("agent.output.invalid", role=role, trace_id=trace_id)
            for _ in range(invalid_attempts)
        ],
        _trace(
            "agent.tool.started",
            role=role,
            trace_id=trace_id,
            fields={
                "execution_id": execution_id,
                "tool": "get_current_run",
                "ordinal": 1,
            },
        ),
        _trace(
            "agent.tool.succeeded",
            role=role,
            trace_id=trace_id,
            fields={
                "execution_id": execution_id,
                "tool": "get_current_run",
                "evidence_count": 0,
            },
        ),
        _trace("agent.turn.finished", role=role, trace_id=trace_id),
    ]
    authority = SimpleNamespace(
        job=SimpleNamespace(tenant_id="tenant_history", command_id="cmd_history"),
        turn=SimpleNamespace(turn_id="turn_history"),
        context=SimpleNamespace(trace_id=trace_id),
    )

    async def validate_tool_summary(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(run_outcomes, "_validate_tool_summary", validate_tool_summary)
    asyncio.run(
        run_outcomes.validate_agent_decision_runtime_authority(
            cast(Any, _TraceSession(_trace_rows(traces))),
            authority=cast(Any, authority),
            receipts=cast(Any, receipts),
            decision={
                "draft": {"role": role},
                "runtime_warnings": [],
                "tool_calls": [{**_DURABLE_CALL, "execution_id": execution_id}],
            },
        )
    )
