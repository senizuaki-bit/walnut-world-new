"""Application orchestrator around deterministic routing and durable commit."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from yaya_agent_contracts import OperationContext

from .context_builder import ContextBuilder
from .domain import (
    AgentDecision,
    AgentTurnClaimReceipt,
    AgentTurnCommitReceipt,
    CommittedAgentTurn,
    GameEvent,
    RoleRoute,
    SkillInvocationResult,
    SkillRecoveryContext,
)
from .errors import AgentContextError, AgentPersistenceError
from .ports import AgentTurnCommitPort, SkillInvocationPort
from .router import RoleRouter
from .runtime import SharedAgentRuntime
from .tool_registry import side_effect_execution_id


@dataclass(frozen=True, slots=True)
class AgentHubResult:
    route: RoleRoute
    decision: AgentDecision | None
    persisted: bool
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.route.should_run:
            if self.decision is None or not self.persisted:
                raise ValueError("handled Agent route requires one persisted decision")
        elif self.decision is not None or self.persisted:
            raise ValueError("no-action route cannot contain a decision")
        if self.replayed and not self.persisted:
            raise ValueError("only a persisted Agent turn can be replayed")


class AgentHub:
    def __init__(
        self,
        *,
        router: RoleRouter,
        contexts: ContextBuilder,
        runtime: SharedAgentRuntime,
        turns: AgentTurnCommitPort,
        invocations: SkillInvocationPort | None = None,
    ) -> None:
        self._router = router
        self._contexts = contexts
        self._runtime = runtime
        self._turns = turns
        self._invocations = invocations

    async def handle(
        self,
        event: GameEvent,
        operation_context: OperationContext,
    ) -> AgentHubResult:
        if event.command_id != operation_context.command_id:
            raise AgentContextError(
                "HUB_COMMAND_MISMATCH",
                "event command_id does not match OperationContext",
            )
        if event.student_id != operation_context.actor.actor_id:
            raise AgentContextError(
                "HUB_ACTOR_MISMATCH",
                "event student_id does not match the authenticated actor",
            )
        try:
            committed = await self._turns.get_committed(event, operation_context)
        except AgentPersistenceError:
            raise
        except Exception as error:
            raise AgentPersistenceError(
                "AGENT_TURN_LOOKUP_FAILED",
                "Agent turn replay lookup failed before side effects",
                {"exception_type": type(error).__name__},
            ) from error
        if committed is not None:
            canonical = _validate_committed_turn(committed, event, operation_context)
            return AgentHubResult(canonical.route, canonical.decision, True, True)

        route = self._router.route(event)
        if not route.should_run:
            return AgentHubResult(route, None, False)
        if route.role == "xiaohutao" and self._invocations is None:
            raise AgentPersistenceError(
                "AGENT_RECOVERY_PORT_REQUIRED",
                "xiaohutao requires durable Skill receipt reconciliation",
            )
        try:
            claim = await self._turns.claim(event, operation_context)
        except AgentPersistenceError:
            raise
        except Exception as error:
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_FAILED",
                "Agent turn could not acquire durable single-flight ownership",
                {"exception_type": type(error).__name__},
            ) from error
        if not isinstance(claim, AgentTurnClaimReceipt):
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_MISMATCH",
                "turn store returned a value outside AgentTurnClaimReceipt",
            )
        if claim.record is not None:
            canonical = _validate_committed_turn(claim.record, event, operation_context)
            return AgentHubResult(canonical.route, canonical.decision, True, True)
        claim_id = claim.claim_id
        if claim_id is None:
            raise AssertionError("validated Agent turn claim must contain claim_id")
        role = route.role
        if role is None:
            raise AssertionError("should_run route must contain a role")
        if role == "xiaohutao":
            recovered = await self._lookup_skill_receipt(event, operation_context)
            if recovered is not None:
                scope = await self._contexts.build_skill_recovery(event, operation_context)
                decision = await self._runtime.recover_skill_invocation(
                    scope,
                    recovered,
                    operation_context,
                )
                return await self._commit_decision(
                    event,
                    route,
                    decision,
                    claim_id,
                    operation_context,
                )

        try:
            context = await self._contexts.build(event, role, operation_context)
        except Exception as error:
            await self._abandon_pre_side_effect_claim(
                event,
                claim_id,
                operation_context,
                original_error=error,
            )
            if isinstance(error, AgentContextError):
                raise
            raise AgentContextError(
                "AGENT_CONTEXT_BUILD_FAILED",
                "Agent context construction failed before any model or tool call",
                {"exception_type": type(error).__name__},
            ) from error
        if role == "xiaohutao":
            renewed = await self._renew_claim_for_runtime(
                event,
                claim_id,
                operation_context,
            )
            if renewed.record is not None:
                canonical = _validate_committed_turn(
                    renewed.record,
                    event,
                    operation_context,
                )
                return AgentHubResult(canonical.route, canonical.decision, True, True)
        decision = await self._runtime.run(role, context, operation_context)
        if role == "xiaohutao" and decision.degraded:
            recovered = await self._lookup_skill_receipt(event, operation_context)
            if recovered is None and "SIDE_EFFECT_COMMIT_UNKNOWN" in decision.runtime_warnings:
                recovered = await self._await_skill_receipt(event, operation_context)
            if recovered is not None:
                if context.skill is None:
                    raise AssertionError("validated xiaohutao context must contain a Skill")
                scope = SkillRecoveryContext(
                    event,
                    context.task,
                    context.session,
                    context.skill,
                )
                decision = await self._runtime.recover_skill_invocation(
                    scope,
                    recovered,
                    operation_context,
                    runtime_warnings=decision.runtime_warnings,
                )
            elif "SIDE_EFFECT_COMMIT_UNKNOWN" in decision.runtime_warnings:
                raise AgentPersistenceError(
                    "UNKNOWN_COMMIT_STATE",
                    "Skill invocation was dispatched but no canonical receipt is visible yet",
                    {
                        "invocation_id": side_effect_execution_id(
                            event.command_id,
                            event.turn_id,
                        )
                    },
                )
            elif "SIDE_EFFECT_ROLLED_BACK" in decision.runtime_warnings:
                # The invocation adapter received an explicit PostgreSQL
                # rollback acknowledgement after Sandbox execution.  No World,
                # Run, Evidence or receipt exists, so release the single-flight
                # claim and retry the same deterministic invocation identity.
                await self._abandon_pre_side_effect_claim(
                    event,
                    claim_id,
                    operation_context,
                    original_error=AgentPersistenceError(
                        "SIDE_EFFECT_ROLLED_BACK",
                        "Skill persistence was explicitly rolled back",
                    ),
                )
                raise AgentPersistenceError(
                    "SIDE_EFFECT_ROLLED_BACK",
                    "Skill persistence was explicitly rolled back and must be retried",
                )
        return await self._commit_decision(
            event,
            route,
            decision,
            claim_id,
            operation_context,
        )

    async def _lookup_skill_receipt(
        self,
        event: GameEvent,
        operation_context: OperationContext,
    ) -> SkillInvocationResult | None:
        invocations = self._invocations
        if invocations is None:
            raise AssertionError("xiaohutao recovery Port was validated before lookup")
        invocation_id = side_effect_execution_id(event.command_id, event.turn_id)
        try:
            result = await invocations.get_result(invocation_id, operation_context)
        except AgentPersistenceError:
            raise
        except Exception as error:
            raise AgentPersistenceError(
                "AGENT_SKILL_RECEIPT_LOOKUP_FAILED",
                "durable Skill receipt lookup failed before Agent turn commit",
                {"exception_type": type(error).__name__},
            ) from error
        if result is not None and not isinstance(result, SkillInvocationResult):
            raise AgentPersistenceError(
                "AGENT_SKILL_RECEIPT_MISMATCH",
                "Skill receipt lookup returned a value outside SkillInvocationResult",
            )
        if result is not None and result.invocation_id != invocation_id:
            raise AgentPersistenceError(
                "AGENT_SKILL_RECEIPT_IDENTITY_MISMATCH",
                "Skill receipt lookup returned another idempotency identity",
            )
        return result

    async def _await_skill_receipt(
        self,
        event: GameEvent,
        operation_context: OperationContext,
    ) -> SkillInvocationResult | None:
        # Short bounded reconciliation absorbs ordinary response/replica lag.
        # Longer uncertainty remains explicit and is resumed after lease expiry.
        for delay_seconds in (0.02, 0.04, 0.08, 0.16):
            await asyncio.sleep(delay_seconds)
            result = await self._lookup_skill_receipt(event, operation_context)
            if result is not None:
                return result
        return None

    async def _renew_claim_for_runtime(
        self,
        event: GameEvent,
        claim_id: str,
        operation_context: OperationContext,
    ) -> AgentTurnClaimReceipt:
        try:
            receipt = await self._turns.renew(
                event,
                claim_id,
                self._runtime.execution_budget_ms("xiaohutao"),
                operation_context,
            )
        except AgentPersistenceError:
            raise
        except Exception as error:
            raise AgentPersistenceError(
                "AGENT_TURN_RENEW_FAILED",
                "Agent turn claim could not be fenced for the bounded Runtime",
                {"exception_type": type(error).__name__},
            ) from error
        if not isinstance(receipt, AgentTurnClaimReceipt):
            raise AgentPersistenceError(
                "AGENT_TURN_RENEW_MISMATCH",
                "turn store returned a value outside AgentTurnClaimReceipt",
            )
        if receipt.record is None and receipt.claim_id != claim_id:
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_LOST",
                "turn store changed the fencing token during renewal",
            )
        return receipt

    async def _commit_decision(
        self,
        event: GameEvent,
        route: RoleRoute,
        decision: AgentDecision,
        claim_id: str,
        operation_context: OperationContext,
    ) -> AgentHubResult:
        try:
            receipt = await self._turns.commit(
                event,
                route,
                decision,
                claim_id,
                operation_context,
            )
        except AgentPersistenceError:
            raise
        except Exception as error:
            raise AgentPersistenceError(
                "AGENT_TURN_COMMIT_FAILED",
                "Agent turn could not be committed atomically",
                {"exception_type": type(error).__name__},
            ) from error
        if not isinstance(receipt, AgentTurnCommitReceipt):
            raise AgentPersistenceError(
                "AGENT_TURN_COMMIT_MISMATCH",
                "turn store returned a value outside AgentTurnCommitReceipt",
            )
        _validate_committed_turn(receipt.record, event, operation_context)
        canonical = receipt.record.decision
        if receipt.created and canonical != decision:
            raise AgentPersistenceError(
                "AGENT_TURN_COMMIT_MISMATCH",
                "newly created turn differs from the validated decision",
            )
        return AgentHubResult(receipt.record.route, canonical, True, not receipt.created)

    async def _abandon_pre_side_effect_claim(
        self,
        event: GameEvent,
        claim_id: str,
        operation_context: OperationContext,
        *,
        original_error: Exception,
    ) -> None:
        """Release only failures proven to precede Runtime side effects.

        Runtime and commit failures retain ownership until lease expiry because
        their external commit state may be unknown.  A later worker must first
        replay a canonical record or acquire a fresh CAS claim.
        """

        try:
            await self._turns.abandon(event, claim_id, operation_context)
        except Exception as abandon_error:
            raise AgentPersistenceError(
                "AGENT_TURN_ABANDON_FAILED",
                "Agent turn claim could not be released after a pre-side-effect failure",
                {
                    "original_exception_type": type(original_error).__name__,
                    "abandon_exception_type": type(abandon_error).__name__,
                },
            ) from abandon_error


def _validate_committed_turn(
    value: object,
    event: GameEvent,
    operation_context: OperationContext,
) -> CommittedAgentTurn:
    if not isinstance(value, CommittedAgentTurn):
        raise AgentPersistenceError(
            "AGENT_TURN_REPLAY_INVALID",
            "turn store returned a value outside CommittedAgentTurn",
        )
    if value.event != event:
        raise AgentPersistenceError(
            "AGENT_TURN_REPLAY_IDENTITY_MISMATCH",
            "canonical turn belongs to a different immutable event",
        )
    stored_actor = value.actor
    current_actor = operation_context.actor
    if (
        stored_actor.tenant_id != current_actor.tenant_id
        or stored_actor.actor_id != current_actor.actor_id
        or stored_actor.actor_type is not current_actor.actor_type
    ):
        raise AgentPersistenceError(
            "AGENT_TURN_REPLAY_AUTHORITY_MISMATCH",
            "canonical turn belongs to a different authenticated principal",
        )
    if value.content_ref != operation_context.content_ref:
        raise AgentPersistenceError(
            "AGENT_TURN_REPLAY_CONTENT_MISMATCH",
            "canonical turn belongs to a different pinned content version",
        )
    return value


__all__ = ["AgentHub", "AgentHubResult"]
