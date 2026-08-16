"""Fail-closed public acceptance gates for CurrentSessionBinding authority."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    CommandStatus,
    ContentRef,
    Failure,
    OperationContext,
    Success,
)

from walnut_backend.adapters.postgres.agent_sessions import PostgresAgentSessionStore
from walnut_backend.adapters.postgres.agent_turns import PostgresAgentTurnStore

TENANT_ID = "tenant_binding"
ACTOR_ID = "student_binding"
AUTHORITY_ID = "authority_binding"
SESSION_ID = "session_binding"
CONTENT_HASH = "a" * 64
WORLD_ID = "world_binding"
LEARNER_ID = "learner_binding"
AGENT_PROFILE_ID = "agent_binding"
TIMELINE_START = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)


class _Session:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def begin(self) -> _Session:
        return self

    async def scalar(self, query: object) -> object:
        del query
        if not self._values:
            raise AssertionError("unexpected authority query")
        return self._values.pop(0)


class _Sessions:
    def __init__(self, values: list[object]) -> None:
        self._session = _Session(values)

    def __call__(self) -> _Session:
        return self._session


class _Command:
    def idempotency_scope(self, context: object) -> tuple[str, str, str, str]:
        del context
        return TENANT_ID, ACTOR_ID, "TEST_OPERATION", "idem_binding"


class _CommandStore:
    def __init__(self, receipt: object) -> None:
        self._receipt = receipt
        self.accept_calls = 0

    async def accept_once_in_session(self, *args: object) -> Success[object]:
        del args
        self.accept_calls += 1
        return Success(self._receipt)


def _context(*, requested_at: datetime | None = None) -> OperationContext:
    return OperationContext(
        request_id="req_binding_0001",
        correlation_id="corr_binding_0001",
        trace_id="trace_binding_0001",
        requested_at=requested_at or TIMELINE_START + timedelta(seconds=3),
        actor=ActorRef(TENANT_ID, ACTOR_ID, ActorType.STUDENT, ("student",)),
        content_ref=ContentRef("UNIT_BINDING_001", "1.0.0", CONTENT_HASH),
        command_id="cmd_binding_request_0001",
        causation_id=None,
    )


def _owner() -> Any:
    return SimpleNamespace(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        command_id="cmd_binding",
        world_id=WORLD_ID,
        status="ACTIVE",
        created_at=TIMELINE_START + timedelta(seconds=1),
        session_json={
            "content": {
                "unit_id": "UNIT_BINDING_001",
                "version": "1.0.0",
                "content_hash": CONTENT_HASH,
            },
            "learner_id": LEARNER_ID,
            "agent_profile_id": AGENT_PROFILE_ID,
            "channel": "GAME",
            "last_turn_sequence": 0,
        },
    )


def _authority() -> Any:
    return SimpleNamespace(
        tenant_id=TENANT_ID,
        authority_id=AUTHORITY_ID,
        actor_id=ACTOR_ID,
        content_unit_id="UNIT_BINDING_001",
        content_version="1.0.0",
        content_hash=CONTENT_HASH,
        world_id=WORLD_ID,
        learner_id=LEARNER_ID,
        agent_profile_id=AGENT_PROFILE_ID,
        channel="GAME",
        created_at=TIMELINE_START,
        active=True,
    )


def _binding(tamper: str) -> Any:
    digest = hashlib.sha256(
        "\x00".join(("binding", TENANT_ID, AUTHORITY_ID, SESSION_ID)).encode("utf-8")
    ).hexdigest()
    return SimpleNamespace(
        binding_id=("binding_tampered" if tamper == "binding_id" else f"binding_{digest[:24]}"),
        tenant_id=("tenant_tampered" if tamper == "tenant_id" else TENANT_ID),
        authority_id=("authority_tampered" if tamper == "authority_id" else AUTHORITY_ID),
        session_id=("session_tampered" if tamper == "session_id" else SESSION_ID),
        actor_id=ACTOR_ID,
        content_hash=CONTENT_HASH,
        world_id=WORLD_ID,
        learner_id=LEARNER_ID,
        agent_profile_id=AGENT_PROFILE_ID,
        bound_at=(
            TIMELINE_START - timedelta(seconds=1)
            if tamper == "bound_at"
            else (
                datetime(2026, 8, 13, 8, 0, 2)
                if tamper == "bound_at_naive"
                else (
                    TIMELINE_START + timedelta(seconds=4)
                    if tamper == "bound_at_future"
                    else TIMELINE_START + timedelta(seconds=2)
                )
            )
        ),
    )


def _receipt() -> Any:
    return SimpleNamespace(
        created=False,
        command=SimpleNamespace(
            command_id="cmd_binding",
            status=CommandStatus.APPLIED,
            terminal=True,
        ),
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "binding_id",
        "tenant_id",
        "authority_id",
        "session_id",
        "bound_at",
        "bound_at_naive",
        "bound_at_future",
    ],
)
def test_session_acceptance_replay_rejects_tampered_current_binding(tamper: str) -> None:
    async def exercise() -> None:
        receipt = _receipt()
        commands = _CommandStore(receipt)
        store = PostgresAgentSessionStore(
            cast(
                Any,
                _Sessions(
                    [
                        TIMELINE_START + timedelta(seconds=3),
                        object(),
                        _owner(),
                        _binding(tamper),
                        _authority(),
                    ]
                ),
            ),
            cast(Any, commands),
            cast(Any, object()),
        )

        result = await store.accept(
            cast(Any, _Command()),
            {
                "world_id": WORLD_ID,
                "learner_id": LEARNER_ID,
                "agent_profile_id": AGENT_PROFILE_ID,
                "channel": "GAME",
            },
            _context(),
        )

        assert isinstance(result, Failure)
        assert result.error.code == "INVARIANT_VIOLATION"
        assert commands.accept_calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "tamper",
    [
        "binding_id",
        "tenant_id",
        "authority_id",
        "session_id",
        "bound_at",
        "bound_at_naive",
        "bound_at_future",
    ],
)
def test_turn_acceptance_replay_rejects_tampered_current_binding(tamper: str) -> None:
    async def exercise() -> None:
        receipt = _receipt()
        commands = _CommandStore(receipt)
        store = PostgresAgentTurnStore(
            cast(
                Any,
                _Sessions(
                    [
                        TIMELINE_START + timedelta(seconds=3),
                        object(),
                        _owner(),
                        _binding(tamper),
                        _authority(),
                    ]
                ),
            ),
            cast(Any, commands),
            cast(Any, object()),
        )

        result = await store.accept(
            SESSION_ID,
            cast(Any, _Command()),
            {},
            _context(),
        )

        assert isinstance(result, Failure)
        assert result.error.code == "INVARIANT_VIOLATION"
        assert commands.accept_calls == 0

    asyncio.run(exercise())


def test_valid_current_binding_preserves_session_and_turn_replay() -> None:
    async def exercise() -> None:
        session_receipt = _receipt()
        session_commands = _CommandStore(session_receipt)
        session_store = PostgresAgentSessionStore(
            cast(
                Any,
                _Sessions(
                    [
                        TIMELINE_START + timedelta(seconds=3),
                        object(),
                        _owner(),
                        _binding("none"),
                        _authority(),
                    ]
                ),
            ),
            cast(Any, session_commands),
            cast(Any, object()),
        )
        session_result = await session_store.accept(
            cast(Any, _Command()),
            {
                "world_id": WORLD_ID,
                "learner_id": LEARNER_ID,
                "agent_profile_id": AGENT_PROFILE_ID,
                "channel": "GAME",
            },
            _context(),
        )

        turn_receipt = _receipt()
        turn_commands = _CommandStore(turn_receipt)
        turn_store = PostgresAgentTurnStore(
            cast(
                Any,
                _Sessions(
                    [
                        TIMELINE_START + timedelta(seconds=3),
                        object(),
                        _owner(),
                        _binding("none"),
                        _authority(),
                    ]
                ),
            ),
            cast(Any, turn_commands),
            cast(Any, object()),
        )
        turn_result = await turn_store.accept(
            SESSION_ID,
            cast(Any, _Command()),
            {},
            _context(),
        )

        assert isinstance(session_result, Success)
        assert isinstance(turn_result, Success)
        assert session_commands.accept_calls == 1
        assert turn_commands.accept_calls == 1

    asyncio.run(exercise())


def test_database_observation_allows_legitimate_binding_despite_host_clock_skew() -> None:
    async def exercise() -> None:
        receipt = _receipt()
        commands = _CommandStore(receipt)
        store = PostgresAgentTurnStore(
            cast(
                Any,
                _Sessions(
                    [
                        TIMELINE_START + timedelta(seconds=3),
                        object(),
                        _owner(),
                        _binding("none"),
                        _authority(),
                    ]
                ),
            ),
            cast(Any, commands),
            cast(Any, object()),
        )

        result = await store.accept(
            SESSION_ID,
            cast(Any, _Command()),
            {},
            _context(requested_at=TIMELINE_START + timedelta(seconds=1, microseconds=500_000)),
        )

        assert isinstance(result, Success)
        assert commands.accept_calls == 1

    asyncio.run(exercise())
