"""Narrow internal ports used by the Agent application layer.

The public cross-module ports remain authoritative in
``yaya_agent_contracts.ports``.  These read-model ports fill the application
gap identified by the integration specification without importing an ORM,
HTTP client, database model or provider SDK into the runtime.
"""

from __future__ import annotations

from typing import Protocol

from yaya_agent_contracts import OperationContext, SkillRef

from .domain import (
    AgentDecision,
    AgentTraceEvent,
    AgentTurnClaimReceipt,
    AgentTurnCommitReceipt,
    CommittedAgentTurn,
    CompileResultSnapshot,
    CounterexampleSnapshot,
    DraftSnapshot,
    FailedInteractionSnapshot,
    GameEvent,
    LearnerProfileSnapshot,
    MessageSnapshot,
    RoleRoute,
    RunResultSnapshot,
    SessionSnapshot,
    SkillInvocationRequest,
    SkillInvocationResult,
    SkillSnapshot,
    SkillVersionSummary,
    TaskSnapshot,
)


class TaskReadPort(Protocol):
    async def get_task(self, task_id: str, context: OperationContext) -> TaskSnapshot: ...


class SessionReadPort(Protocol):
    async def get_session(
        self,
        session_id: str,
        context: OperationContext,
    ) -> SessionSnapshot: ...


class SkillReadPort(Protocol):
    async def get_bound_skill(
        self,
        skill_ref: SkillRef,
        context: OperationContext,
    ) -> SkillSnapshot: ...

    async def list_active_skills(
        self,
        student_id: str,
        context: OperationContext,
    ) -> tuple[SkillSnapshot, ...]: ...

    async def list_skill_history(
        self,
        skill_id: str,
        session_id: str,
        context: OperationContext,
    ) -> tuple[SkillVersionSummary, ...]: ...


class DraftReadPort(Protocol):
    """Read only the exact current Draft; Agent never owns a Draft write use case."""

    async def get_current_draft(
        self,
        session_id: str,
        draft_id: str,
        context: OperationContext,
    ) -> DraftSnapshot: ...


class InteractionReadPort(Protocol):
    """Resolve the selected latest failed Product Interaction, read only.

    Implementations must validate the canonical projection receipt and
    feedback event, and reject an interaction outside the current
    same-failure suffix before returning this snapshot.
    """

    async def get_current_failed_interaction(
        self,
        session_id: str,
        interaction_id: str,
        context: OperationContext,
    ) -> FailedInteractionSnapshot: ...


class RunReadPort(Protocol):
    """Canonical run reads; history tuples are ordered oldest to newest.

    ``through_run_id`` must be the final tuple item. Run, turn and command IDs
    are unique within each returned history.
    """

    async def get_compile_result(
        self,
        build_id: str,
        context: OperationContext,
    ) -> CompileResultSnapshot: ...

    async def get_run(
        self,
        run_id: str,
        context: OperationContext,
    ) -> RunResultSnapshot: ...

    async def list_same_failure_runs(
        self,
        session_id: str,
        failure_key: str,
        through_run_id: str,
        limit: int,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]: ...

    async def list_session_runs(
        self,
        session_id: str,
        through_run_id: str,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]: ...


class CounterexampleReadPort(Protocol):
    async def list_counterexamples(
        self,
        task_id: str,
        failure_key: str,
        context: OperationContext,
    ) -> tuple[CounterexampleSnapshot, ...]: ...


class LearnerReadPort(Protocol):
    async def get_profile(
        self,
        student_id: str,
        knowledge_points: tuple[str, ...],
        context: OperationContext,
    ) -> LearnerProfileSnapshot: ...


class MessageReadPort(Protocol):
    async def list_recent(
        self,
        session_id: str,
        limit: int,
        context: OperationContext,
    ) -> tuple[MessageSnapshot, ...]: ...


class SkillInvocationPort(Protocol):
    """Application use case that owns Sandbox -> World -> Evidence execution.

    The durable idempotency scope is ``(tenant_id, invocation_id)``.  Equal
    ``request_sha256`` replays the original result; a different hash for that
    identity must fail with an explicit conflict before another World write.
    The result echoes tenant and request hash so callers can reject stale or
    cross-tenant receipts even when all resource IDs happen to match.
    Implementations construct and validate the bounded result plus immutable
    Evidence before atomically publishing World/Run/Evidence/the idempotency
    receipt. A consumer-side size check cannot repair commit-before-serialize.
    """

    async def invoke(
        self,
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> SkillInvocationResult: ...

    async def get_result(
        self,
        invocation_id: str,
        context: OperationContext,
    ) -> SkillInvocationResult | None:
        """Return the canonical receipt after response loss or worker takeover.

        The lookup scope is the authenticated tenant plus ``invocation_id``.
        Implementations persist this receipt atomically with World/Run/Evidence;
        returning ``None`` after the World advanced is a contract violation.
        """
        ...


class AgentTurnCommitPort(Protocol):
    """Idempotently persist feedback/event and enqueue projection work.

    Implementations key the record by authenticated tenant plus the complete
    immutable ``GameEvent`` identity, and persist the originating actor and
    pinned content reference in ``CommittedAgentTurn``. ``get_committed`` runs
    before routing so historical decisions survive policy drift. For a handled
    route, ``claim`` atomically acquires a bounded single-flight lease or
    returns a concurrent committed winner before any model or side-effect
    tool. A live claim conflict must fail explicitly without running the
    Runtime. Expired leases are replaced with a fresh, fencing ``claim_id``;
    commit and abandon are compare-and-set operations on that token, so an old
    worker can never publish after takeover. ``abandon`` is used only for a
    failure proven to precede Runtime side effects. Runtime/commit uncertainty
    is recovered by canonical replay or lease expiry instead.
    """

    async def get_committed(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> CommittedAgentTurn | None: ...

    async def claim(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> AgentTurnClaimReceipt: ...

    async def abandon(
        self,
        event: GameEvent,
        claim_id: str,
        context: OperationContext,
    ) -> None: ...

    async def renew(
        self,
        event: GameEvent,
        claim_id: str,
        minimum_ttl_ms: int,
        context: OperationContext,
    ) -> AgentTurnClaimReceipt: ...

    async def commit(
        self,
        event: GameEvent,
        route: RoleRoute,
        decision: AgentDecision,
        claim_id: str,
        context: OperationContext,
    ) -> AgentTurnCommitReceipt: ...


class AgentTracePort(Protocol):
    """Durable or operational trace sink; hidden model reasoning is never recorded."""

    async def record(
        self,
        event: AgentTraceEvent,
        context: OperationContext,
    ) -> None: ...


__all__ = [
    "AgentTracePort",
    "AgentTurnCommitPort",
    "CounterexampleReadPort",
    "DraftReadPort",
    "LearnerReadPort",
    "MessageReadPort",
    "RunReadPort",
    "SessionReadPort",
    "SkillInvocationPort",
    "SkillReadPort",
    "TaskReadPort",
]
