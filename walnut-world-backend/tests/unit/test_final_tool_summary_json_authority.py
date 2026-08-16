"""Final tool summaries compare at the durable JSON authority boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from yaya_agent_runtime import RunResultSnapshot

from walnut_backend.adapters.postgres import run_outcomes
from walnut_backend.adapters.postgres.models import json_value
from walnut_backend.adapters.postgres.workflow_jobs import WorkflowInvariantError


def _failed_run(
    run_id: str,
    *,
    failed_actions: tuple[dict[str, object], ...],
) -> RunResultSnapshot:
    return cast(
        RunResultSnapshot,
        SimpleNamespace(
            run_id=run_id,
            session_id="session_tool_summary",
            task_success=False,
            world_revision_before=7,
            world_revision_after=7,
            world_difference={"changes": ({"path": ("farm", "water")},)},
            failed_actions=failed_actions,
            failure_key="objective_not_met",
            evidence_refs=(),
        ),
    )


def _durable_run_summary(run: RunResultSnapshot) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json_value(
            run_outcomes._run_tool_projection(run)  # pyright: ignore[reportPrivateUsage]
        ),
    )


@pytest.mark.parametrize(
    "failed_actions",
    (
        (),
        ({"action": "watering", "observed": ("dry", "unchanged")},),
    ),
)
def test_get_current_run_accepts_json_normalized_tuple_fields(
    failed_actions: tuple[dict[str, object], ...],
) -> None:
    run = _failed_run("run_current_summary", failed_actions=failed_actions)
    summary = _durable_run_summary(run)

    assert isinstance(summary["failed_actions"], list)
    assert isinstance(summary["world_difference"]["changes"], list)
    assert isinstance(summary["world_difference"]["changes"][0]["path"], list)

    asyncio.run(
        run_outcomes._validate_tool_summary(  # pyright: ignore[reportPrivateUsage]
            cast(Any, None),
            authority=cast(Any, SimpleNamespace(run=run)),
            tool={
                "name": "get_current_run",
                "arguments": {},
                "result_summary": summary,
            },
        )
    )


def test_get_current_run_rejects_tampered_json_value() -> None:
    run = _failed_run(
        "run_current_tamper",
        failed_actions=({"action": "watering", "observed": ("dry",)},),
    )
    summary = _durable_run_summary(run)
    summary["failed_actions"][0]["action"] = "tampered"

    with pytest.raises(WorkflowInvariantError, match="tool result summary drifted"):
        asyncio.run(
            run_outcomes._validate_tool_summary(  # pyright: ignore[reportPrivateUsage]
                cast(Any, None),
                authority=cast(Any, SimpleNamespace(run=run)),
                tool={
                    "name": "get_current_run",
                    "arguments": {},
                    "result_summary": summary,
                },
            )
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("task_success", 0),
        ("world_revision_before", 7.0),
    ),
)
def test_get_current_run_rejects_equal_but_differently_typed_json_scalar(
    field: str,
    replacement: object,
) -> None:
    run = _failed_run("run_current_scalar_type", failed_actions=())
    summary = _durable_run_summary(run)
    assert summary[field] == replacement
    assert type(summary[field]) is not type(replacement)
    summary[field] = replacement

    with pytest.raises(WorkflowInvariantError, match="tool result summary drifted"):
        asyncio.run(
            run_outcomes._validate_tool_summary(  # pyright: ignore[reportPrivateUsage]
                cast(Any, None),
                authority=cast(Any, SimpleNamespace(run=run)),
                tool={
                    "name": "get_current_run",
                    "arguments": {},
                    "result_summary": summary,
                },
            )
        )


def test_get_session_runs_accepts_multiple_json_normalized_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _failed_run("run_session_first", failed_actions=())
    second = _failed_run(
        "run_session_second",
        failed_actions=({"action": "watering", "observed": ("dry",)},),
    )
    context = object()

    async def list_runs(
        session: object,
        *,
        session_id: str,
        through_run_id: str,
        context: object,
        validation_state: object | None = None,
    ) -> tuple[RunResultSnapshot, ...]:
        assert session is fake_session
        assert session_id == second.session_id
        assert through_run_id == second.run_id
        assert context is expected_context
        assert isinstance(
            validation_state,
            run_outcomes.TerminalProjectionValidationState,
        )
        return (first, second)

    fake_session = object()
    expected_context = context
    monkeypatch.setattr(run_outcomes, "list_validated_session_runs", list_runs)
    summary = cast(
        dict[str, Any],
        json_value(
            {
                "runs": [
                    run_outcomes._run_tool_projection(first),  # pyright: ignore[reportPrivateUsage]
                    run_outcomes._run_tool_projection(second),  # pyright: ignore[reportPrivateUsage]
                ]
            }
        ),
    )

    assert len(summary["runs"]) == 2
    assert isinstance(summary["runs"][0]["failed_actions"], list)
    assert isinstance(summary["runs"][1]["world_difference"]["changes"], list)

    asyncio.run(
        run_outcomes._validate_tool_summary(  # pyright: ignore[reportPrivateUsage]
            cast(Any, fake_session),
            authority=cast(
                Any,
                SimpleNamespace(
                    run=second,
                    context=expected_context,
                ),
            ),
            tool={
                "name": "get_session_runs",
                "arguments": {},
                "result_summary": summary,
            },
        )
    )
