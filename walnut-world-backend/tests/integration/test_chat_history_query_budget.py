"""Bound repeated history validation inside one public interaction read.

Use a real failed Run followed by ordinary chat, but fixed provider replies and
the existing deterministic sandbox fixtures. SQL counts exclude all setup and
turn execution, and timings are diagnostic rather than machine-speed asserts.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from yaya_agent_contracts import Success

from tests.integration import test_hint_vertical as hint
from tests.integration.test_int2_patch_vertical import (
    _materialize_failure_chain,
    _restore_evidence_payload,
    _tamper_evidence_payload,
)
from walnut_backend.adapters.postgres import product_interactions
from walnut_backend.adapters.postgres.models import ProductInteractionRow
from walnut_backend.api.app import create_app


@dataclass
class _ReadCost:
    sql: int = 0
    hint_checks: int = 0
    seconds: float = 0


@contextmanager
def _measure_read() -> Iterator[_ReadCost]:
    """Count actual validator bodies, including recursive calls on older rows."""
    cost = _ReadCost()
    original = product_interactions._hint_interaction_has_authority

    def count_query(*args: Any) -> None:
        cost.sql += 1

    async def count_hint(*args: Any, **kwargs: Any) -> bool:
        cost.hint_checks += 1
        return await original(*args, **kwargs)

    event.listen(Engine, "before_cursor_execute", count_query)
    started = time.monotonic()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(product_interactions, "_hint_interaction_has_authority", count_hint)
            yield cost
    finally:
        cost.seconds = time.monotonic() - started
        event.remove(Engine, "before_cursor_execute", count_query)


def _assert_bounded(cost: _ReadCost, chat_count: int, read_kind: str) -> None:
    print(
        json.dumps(
            {
                "chat_count": chat_count,
                "read_kind": read_kind,
                "sql": cost.sql,
                "hint_checks": cost.hint_checks,
                "seconds": round(cost.seconds, 3),
            }
        ),
        flush=True,
    )
    # Each earlier chat must still be checked, but only once per request. This
    # also catches accidentally persisting successful validation across reads.
    assert cost.hint_checks == chat_count
    # The failed Run has a fixed source-validation cost of roughly 500 queries.
    # Allow modest per-chat growth without permitting the former 13,546-query
    # five-chat traversal; wall-clock duration is intentionally not asserted.
    assert cost.sql <= 600 + 50 * chat_count


@pytest.mark.parametrize("chat_count", [2, 5])
def test_failed_run_chat_reads_validate_history_once_per_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_count: int,
) -> None:
    database_url = hint._database_url()
    with TestClient(create_app(hint._settings(database_url))) as client:
        terminal = hint._execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        operation = hint._student_operation(client, terminal)
        chain = _materialize_failure_chain(client, terminal, operation, count=1)
        app = cast(Any, client.app)
        turn_ids = []
        for index in range(1, chat_count + 1):
            suffix = terminal.build_id[-16:]
            turn_id = f"turn_chat_budget_{index}_{suffix}"
            turn_ids.append(turn_id)
            turn_operation = replace(
                operation,
                command_id=f"cmd_chat_budget_{index}_{suffix}",
                causation_id=None,
            )
            payload = hint._hint_payload(
                revision=chain.world_revision,
                sequence=chain.world_sequence,
                turn_sequence=chain.next_turn_sequence + index - 1,
                turn_id=turn_id,
                skill_bindings=[],
            )
            payload["input"]["text"] = "你好，你是谁？"
            accepted = hint._portal_call(
                client,
                app.state.agent_turns.accept,
                chain.session_id,
                json.dumps(payload).encode("utf-8"),
                f"idem_chat_budget_{index}_{suffix}",
                turn_operation,
            )
            assert isinstance(accepted, Success), accepted
            hint._run_hint_turn(
                client, terminal, turn_operation, accepted, hint._HintReplyProvider("message")
            )

        path = f"/product-experience/v1/sessions/{chain.session_id}/agent-interactions"
        with _measure_read() as full_cost:
            response = client.get(path, headers=terminal.headers)
        assert response.status_code == 200, response.text
        rows = response.json()["interactions"]
        assert len(rows) == chat_count + 1
        assert [row["turn_id"] for row in rows[1:]] == turn_ids
        assert all(row["response_type"] == "message" for row in rows[1:])
        assert all(row["feedback"]["run_id"] is not None for row in rows)
        _assert_bounded(full_cost, chat_count, "full-list")

        # The latest-only page must traverse earlier dependencies omitted from
        # the page. Its new database snapshot must validate them all anew.
        latest_params = {"after_sequence": rows[-1]["sequence"] - 1, "limit": 1}
        with _measure_read() as page_cost:
            page = client.get(path, headers=terminal.headers, params=latest_params)
        assert page.status_code == 200, page.text
        assert page.json()["interactions"] == [rows[-1]]
        _assert_bounded(page_cost, chat_count, "latest-page")

        with _measure_read() as single_cost:
            single = client.get(f"{path}/{rows[-1]['interaction_id']}", headers=terminal.headers)
        assert single.status_code == 200, single.text
        assert single.json() == rows[-1]
        _assert_bounded(single_cost, chat_count, "single-resource")

        # A previously verified historical row must not be trusted in a later
        # request, even when that row is outside the requested page.
        original_history = hint._portal_call(
            client, _change_history_message, terminal, rows[1]["interaction_id"], None
        )
        drifted = client.get(path, headers=terminal.headers, params=latest_params)
        assert drifted.status_code == 500, drifted.text
        assert drifted.json()["error"]["code"] == "INVARIANT_VIOLATION"
        hint._portal_call(
            client,
            _change_history_message,
            terminal,
            rows[1]["interaction_id"],
            original_history,
        )
        assert client.get(path, headers=terminal.headers, params=latest_params).status_code == 200

        # Reusing a failed Run within a request must preserve validation of its
        # durable evidence; changing its source is detected by the next read.
        evidence_id = chain.interactions[0]["feedback"]["evidence_refs"][0]["evidence_id"]
        original_evidence = hint._portal_call(
            client, _tamper_evidence_payload, terminal, evidence_id
        )
        drifted_source = client.get(path, headers=terminal.headers, params=latest_params)
        assert drifted_source.status_code == 500, drifted_source.text
        assert drifted_source.json()["error"]["code"] == "INVARIANT_VIOLATION"
        hint._portal_call(
            client, _restore_evidence_payload, terminal, evidence_id, original_evidence
        )
        assert client.get(path, headers=terminal.headers, params=latest_params).status_code == 200


async def _change_history_message(
    terminal: Any, interaction_id: str, replacement: dict[str, Any] | None
) -> dict[str, Any]:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == terminal.tenant_id,
                ProductInteractionRow.actor_id == terminal.actor_id,
                ProductInteractionRow.interaction_id == interaction_id,
            )
        )
        assert row is not None
        original = copy.deepcopy(row.interaction_json)
        value = copy.deepcopy(original if replacement is None else replacement)
        if replacement is None:
            value["feedback"]["message"] = "Changed after the preceding read."
        row.interaction_json = value
        return original
