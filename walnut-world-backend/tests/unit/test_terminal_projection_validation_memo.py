"""Terminal Run history validation must be request-local and linear."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    CommandStatus,
    ContentRef,
    OperationContext,
)

from walnut_backend.adapters.postgres import run_outcomes
from walnut_backend.adapters.postgres.learner_projection_jobs import (
    LearnerProjectionInvariantError,
)
from walnut_backend.adapters.postgres.workflow_jobs import WorkflowInvariantError
from walnut_backend.workers import turn_projection


def _session() -> AsyncSession:
    return cast(AsyncSession, object())


def _context(index: int, *, actor_id: str = "student_0001") -> OperationContext:
    return OperationContext(
        request_id=f"req_memo_{index:04d}",
        correlation_id="corr_memo_0001",
        trace_id="trace_memo_0001",
        requested_at=datetime(2026, 8, 15, tzinfo=UTC),
        actor=ActorRef(
            "tenant_yaya",
            actor_id,
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("UNIT_MEMO", "1.0.0", "a" * 64),
        command_id=f"cmd_memo_{index:04d}",
        causation_id=None,
    )


def _authority(
    index: int,
    *,
    actor_id: str = "student_0001",
    session_id: str = "session_memo_0001",
    turn_id: str | None = None,
    run_id: str | None = None,
    command_id: str | None = None,
) -> Any:
    effective_run_id = run_id or f"run_memo_{index:04d}"
    effective_turn_id = turn_id or f"turn_memo_{index:04d}"
    effective_command_id = command_id or f"cmd_memo_{index:04d}"
    context = _context(index, actor_id=actor_id)
    context = SimpleNamespace(
        **{
            **context.__dict__,
            "command_id": effective_command_id,
        }
    ) if hasattr(context, "__dict__") else OperationContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
        schema_version=context.schema_version,
        command_id=effective_command_id,
        causation_id=context.causation_id,
        deadline_at=context.deadline_at,
    )
    evidence_refs: tuple[Any, ...] = ()
    return SimpleNamespace(
        job=SimpleNamespace(tenant_id="tenant_yaya", status="SUCCEEDED"),
        context=context,
        turn=SimpleNamespace(
            turn_sequence=index,
            turn_id=effective_turn_id,
            command_id=effective_command_id,
        ),
        command=SimpleNamespace(
            terminal=True,
            status=CommandStatus.REJECTED,
            evidence_refs=evidence_refs,
            links={"run": f"/v1/runs/{effective_run_id}"},
        ),
        run=SimpleNamespace(
            session_id=session_id,
            turn_id=effective_turn_id,
            run_id=effective_run_id,
            command_id=effective_command_id,
            task_success=False,
            failure_key="same_failure",
            skill_ref="same_skill",
            world_id="world_memo_0001",
            evidence_refs=evidence_refs,
        ),
    )


def test_four_run_history_validates_each_projection_body_once(monkeypatch: pytest.MonkeyPatch) -> None:
    authorities = tuple(_authority(index) for index in range(1, 5))
    authorities_by_command = {item.run.command_id: item for item in authorities}
    projection_calls: list[str] = []
    load_calls: list[str] = []

    async def load_body(
        _session: object,
        *,
        command_id: str,
        **_kwargs: Any,
    ) -> Any:
        load_calls.append(command_id)
        return authorities_by_command[command_id]

    async def validate_body(
        session: AsyncSession,
        authority: Any,
        *,
        validation_state: Any,
    ) -> None:
        projection_calls.append(authority.run.run_id)
        prior = authorities[: int(authority.run.run_id.rsplit("_", 1)[1]) - 1]
        # Model both real recursive paths: canonical failure suffix and the
        # final decision's get_session_runs tool summary.
        for item in prior:
            await run_outcomes.load_validated_run(
                session,
                tenant_id=item.context.actor.tenant_id,
                actor_id=item.context.actor.actor_id,
                content_hash=item.context.content_ref.content_hash,
                command_id=item.run.command_id,
                expected_context=item.context,
                require_current_world=False,
                validation_state=validation_state,
            )
            await run_outcomes.validate_terminal_projection(
                session, item, validation_state=validation_state
            )
        for item in prior:
            await run_outcomes.load_validated_run(
                session,
                tenant_id=item.context.actor.tenant_id,
                actor_id=item.context.actor.actor_id,
                content_hash=item.context.content_ref.content_hash,
                command_id=item.run.command_id,
                expected_context=item.context,
                require_current_world=False,
                validation_state=validation_state,
            )
            await run_outcomes.validate_terminal_projection(
                session, item, validation_state=validation_state
            )

    monkeypatch.setattr(run_outcomes, "_load_validated_run_uncached", load_body)
    monkeypatch.setattr(run_outcomes, "_validate_terminal_projection_uncached", validate_body)

    async def exercise() -> None:
        session = _session()
        state = run_outcomes.TerminalProjectionValidationState()
        current = authorities[-1]
        loaded = await run_outcomes.load_validated_run(
            session,
            tenant_id=current.context.actor.tenant_id,
            actor_id=current.context.actor.actor_id,
            content_hash=current.context.content_ref.content_hash,
            command_id=current.run.command_id,
            expected_context=current.context,
            require_current_world=False,
            validation_state=state,
        )
        await run_outcomes.validate_terminal_projection(
            session,
            loaded,
            validation_state=state,
        )

    asyncio.run(exercise())

    assert Counter(projection_calls) == Counter(item.run.run_id for item in authorities)
    assert Counter(load_calls) == Counter(item.run.command_id for item in authorities)
    assert len(projection_calls) == len(authorities)
    assert len(load_calls) == len(authorities)


def test_failure_suffix_prefetches_run_presence_in_one_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorities = tuple(_authority(index) for index in range(1, 5))
    by_command = {item.run.command_id: item for item in authorities}

    class Result:
        def all(self) -> list[tuple[Any, str]]:
            return [(item.turn, item.run.command_id) for item in reversed(authorities)]

    class Session:
        execute_calls = 0
        scalar_calls = 0

        async def execute(self, _statement: Any) -> Result:
            self.execute_calls += 1
            return Result()

        async def scalar(self, _statement: Any) -> Any:
            self.scalar_calls += 1
            raise AssertionError("failure suffix must not query Run presence per turn")

    async def load_run(_session: object, *, command_id: str, **_kwargs: Any) -> Any:
        return by_command[command_id]

    async def validate_projection(_session: object, _authority: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(run_outcomes, "load_validated_run", load_run)
    monkeypatch.setattr(run_outcomes, "validate_terminal_projection", validate_projection)
    session = Session()
    count = asyncio.run(
        run_outcomes.exact_failure_suffix_count(
            session,  # type: ignore[arg-type]
            current=authorities[-1],
            context=authorities[-1].context,
            current_must_be_live=False,
        )
    )

    assert count == 4
    assert session.execute_calls == 1
    assert session.scalar_calls == 0


def test_load_cache_is_exact_and_bound_to_one_database_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(1)
    calls: list[tuple[bool, str]] = []

    async def load_body(
        _session: object,
        *,
        require_current_world: bool,
        expected_context: Any,
        **_kwargs: Any,
    ) -> Any:
        calls.append((require_current_world, expected_context.actor.actor_id))
        return authority

    monkeypatch.setattr(run_outcomes, "_load_validated_run_uncached", load_body)

    async def exercise() -> None:
        session = _session()
        state = run_outcomes.TerminalProjectionValidationState()
        kwargs = {
            "tenant_id": authority.context.actor.tenant_id,
            "actor_id": authority.context.actor.actor_id,
            "content_hash": authority.context.content_ref.content_hash,
            "command_id": authority.run.command_id,
            "expected_context": authority.context,
            "require_current_world": False,
            "validation_state": state,
        }
        first = await run_outcomes.load_validated_run(session, **kwargs)
        second = await run_outcomes.load_validated_run(session, **kwargs)
        assert first is second is authority
        await run_outcomes.load_validated_run(
            session,
            **{**kwargs, "require_current_world": True},
        )
        different_actor_context = _context(1, actor_id="student_0002")
        await run_outcomes.load_validated_run(
            session,
            **{**kwargs, "expected_context": different_actor_context},
        )
        with pytest.raises(WorkflowInvariantError, match="database session"):
            await run_outcomes.load_validated_run(_session(), **kwargs)

    asyncio.run(exercise())

    assert calls == [
        (False, "student_0001"),
        (True, "student_0001"),
        (False, "student_0002"),
    ]


def test_failed_validated_run_load_is_not_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _authority(1)
    attempts = 0

    async def load_body(_session: object, **_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WorkflowInvariantError("corrupt Run authority")
        return authority

    monkeypatch.setattr(run_outcomes, "_load_validated_run_uncached", load_body)

    async def exercise() -> None:
        session = _session()
        state = run_outcomes.TerminalProjectionValidationState()
        kwargs = {
            "tenant_id": authority.context.actor.tenant_id,
            "actor_id": authority.context.actor.actor_id,
            "content_hash": authority.context.content_ref.content_hash,
            "command_id": authority.run.command_id,
            "expected_context": authority.context,
            "require_current_world": False,
            "validation_state": state,
        }
        with pytest.raises(WorkflowInvariantError, match="corrupt Run"):
            await run_outcomes.load_validated_run(session, **kwargs)
        loaded = await run_outcomes.load_validated_run(session, **kwargs)
        assert loaded is authority

    asyncio.run(exercise())
    assert attempts == 2


def test_projection_memo_key_includes_actor_session_and_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorities = (
        _authority(1, run_id="run_shared"),
        _authority(1, actor_id="student_0002", run_id="run_shared"),
        _authority(1, session_id="session_memo_0002", run_id="run_shared"),
        _authority(1, turn_id="turn_memo_other", run_id="run_shared"),
        _authority(1, run_id="run_shared", command_id="cmd_memo_other"),
    )
    calls: list[tuple[str, str, str]] = []

    async def validate_body(
        _session: object,
        authority: Any,
        *,
        validation_state: Any,
    ) -> None:
        del validation_state
        calls.append(
            (
                authority.context.actor.actor_id,
                authority.run.session_id,
                authority.run.command_id,
            )
        )

    monkeypatch.setattr(run_outcomes, "_validate_terminal_projection_uncached", validate_body)

    async def exercise() -> None:
        session = _session()
        state = run_outcomes.TerminalProjectionValidationState()
        for authority in authorities:
            await run_outcomes.validate_terminal_projection(
                session,
                authority,
                validation_state=state,
            )

    asyncio.run(exercise())
    assert len(calls) == len(authorities)


def test_frozen_learner_receipt_reuses_terminal_projection_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    state = run_outcomes.TerminalProjectionValidationState()
    captured: list[Any] = []

    async def required_receipt(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(receipt_json={})

    async def validate_outcome(*_args: Any, validation_state: Any, **_kwargs: Any) -> Any:
        captured.append(validation_state)
        raise WorkflowInvariantError("stop after state capture")

    monkeypatch.setattr(turn_projection, "_required_step_receipt", required_receipt)
    monkeypatch.setattr(
        turn_projection,
        "validate_canonical_outcome_event",
        validate_outcome,
    )

    with pytest.raises(WorkflowInvariantError, match="state capture"):
        asyncio.run(
            turn_projection._validate_frozen_a8_receipts(  # pyright: ignore[reportPrivateUsage]
                session,
                claim=cast(Any, object()),
                objective={
                    "outcome": {},
                    "final_decision": {
                        "draft": {},
                        "teaching_directive": {},
                    },
                },
                parent=cast(Any, object()),
                current=cast(Any, object()),
                validation_state=state,
            )
        )

    assert captured == [state]


def test_learner_projection_corruption_translates_to_public_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_corruption(*_args: Any, **_kwargs: Any) -> None:
        raise LearnerProjectionInvariantError("corrupt learner chain")

    monkeypatch.setattr(
        turn_projection,
        "validate_terminal_learner_row_in_session",
        reject_corruption,
    )
    with pytest.raises(WorkflowInvariantError, match="learner projection") as captured:
        asyncio.run(
            run_outcomes._validate_terminal_learner_authority(  # pyright: ignore[reportPrivateUsage]
                _session(),
                authority=cast(Any, object()),
                learner=cast(Any, object()),
                validation_state=run_outcomes.TerminalProjectionValidationState(),
            )
        )

    assert isinstance(captured.value.__cause__, LearnerProjectionInvariantError)


def test_canonical_outcome_memo_is_byte_exact_and_success_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(1)
    calls: list[str] = []
    corrupt_attempts = 0
    expected_event = object()

    async def validate_body(
        _session: object,
        *,
        outcome: dict[str, Any],
        validation_state: Any,
        **_kwargs: Any,
    ) -> Any:
        nonlocal corrupt_attempts
        del validation_state
        marker = str(outcome["marker"])
        calls.append(marker)
        if marker == "corrupt":
            corrupt_attempts += 1
            if corrupt_attempts == 1:
                raise WorkflowInvariantError("corrupt outcome bytes")
        return expected_event

    monkeypatch.setattr(
        run_outcomes,
        "_validate_canonical_outcome_event_uncached",
        validate_body,
    )

    async def exercise() -> None:
        session = _session()
        state = run_outcomes.TerminalProjectionValidationState()
        clean = {"marker": "clean"}
        first = await run_outcomes.validate_canonical_outcome_event(
            session,
            authority=authority,
            outcome=clean,
            validation_state=state,
        )
        second = await run_outcomes.validate_canonical_outcome_event(
            session,
            authority=authority,
            outcome=dict(clean),
            validation_state=state,
        )
        assert first is second is expected_event
        different_identity = _authority(
            1,
            turn_id="turn_memo_other",
            run_id=authority.run.run_id,
        )
        await run_outcomes.validate_canonical_outcome_event(
            session,
            authority=different_identity,
            outcome=dict(clean),
            validation_state=state,
        )
        with pytest.raises(WorkflowInvariantError, match="corrupt outcome"):
            await run_outcomes.validate_canonical_outcome_event(
                session,
                authority=authority,
                outcome={"marker": "corrupt"},
                validation_state=state,
            )
        recovered = await run_outcomes.validate_canonical_outcome_event(
            session,
            authority=authority,
            outcome={"marker": "corrupt"},
            validation_state=state,
        )
        assert recovered is expected_event

    asyncio.run(exercise())
    assert calls == ["clean", "clean", "corrupt", "corrupt"]


def test_canonical_outcome_cycle_fails_closed_and_clears_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(1)
    session = _session()
    state = run_outcomes.TerminalProjectionValidationState()

    async def validate_body(
        _session: AsyncSession,
        *,
        outcome: dict[str, Any],
        validation_state: Any,
        **_kwargs: Any,
    ) -> Any:
        return await run_outcomes.validate_canonical_outcome_event(
            _session,
            authority=authority,
            outcome=outcome,
            validation_state=validation_state,
        )

    monkeypatch.setattr(
        run_outcomes,
        "_validate_canonical_outcome_event_uncached",
        validate_body,
    )

    with pytest.raises(WorkflowInvariantError, match="cycle"):
        asyncio.run(
            run_outcomes.validate_canonical_outcome_event(
                session,
                authority=authority,
                outcome={"marker": "cycle"},
                validation_state=state,
            )
        )

    assert state.canonical_outcome_in_progress == set()
    assert state.canonical_outcomes == {}


def test_projection_cycle_fails_closed_and_clears_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(1)
    session = _session()
    captured: list[Any] = []

    async def validate_body(
        session: AsyncSession,
        current: Any,
        *,
        validation_state: Any,
    ) -> None:
        captured.append(validation_state)
        await run_outcomes.validate_terminal_projection(
            session, current, validation_state=validation_state
        )

    monkeypatch.setattr(run_outcomes, "_validate_terminal_projection_uncached", validate_body)

    with pytest.raises(WorkflowInvariantError, match="cycle"):
        asyncio.run(run_outcomes.validate_terminal_projection(session, authority))

    assert len(captured) == 1
    assert captured[0].in_progress == set()
    assert captured[0].completed == set()


def test_failed_projection_is_not_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _authority(1)
    session = _session()
    attempts = 0
    captured: list[Any] = []

    async def validate_body(
        _session: object,
        _current: Any,
        *,
        validation_state: Any,
    ) -> None:
        nonlocal attempts
        attempts += 1
        captured.append(validation_state)
        if attempts == 1:
            raise WorkflowInvariantError("corrupt projection")

    monkeypatch.setattr(run_outcomes, "_validate_terminal_projection_uncached", validate_body)
    state = run_outcomes.TerminalProjectionValidationState()

    with pytest.raises(WorkflowInvariantError, match="corrupt projection"):
        asyncio.run(
            run_outcomes.validate_terminal_projection(
                session, authority, validation_state=state
            )
        )
    asyncio.run(
        run_outcomes.validate_terminal_projection(session, authority, validation_state=state)
    )

    assert attempts == 2
    assert captured == [state, state]
    assert state.in_progress == set()
    assert state.completed == {
        (
            "tenant_yaya",
            "student_0001",
            "a" * 64,
            "session_memo_0001",
            "turn_memo_0001",
            "run_memo_0001",
            "cmd_memo_0001",
        )
    }
