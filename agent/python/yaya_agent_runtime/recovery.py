"""Deterministic reconciliation for a committed Skill side effect."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from yaya_agent_contracts import EvidenceRef, OperationContext

from .domain import (
    AgentDecision,
    DecisionDraft,
    SkillInvocationResult,
    SkillRecoveryContext,
    ToolCallRecord,
)
from .errors import AgentPersistenceError


def recover_skill_invocation_decision(
    scope: SkillRecoveryContext,
    result: SkillInvocationResult,
    operation_context: OperationContext,
    *,
    completed_at: datetime,
    runtime_warnings: tuple[str, ...] = (),
) -> AgentDecision:
    """Validate a durable receipt and render feedback without rerunning a model."""

    event = scope.event
    run = result.run
    if result.tenant_id != operation_context.actor.tenant_id:
        raise _recovery_error(
            "AGENT_RECOVERY_TENANT_MISMATCH",
            "Skill receipt belongs to another authenticated tenant",
        )
    run_actor = run.request_context.actor
    operation_actor = operation_context.actor
    if (
        run_actor.tenant_id,
        run_actor.actor_id,
        run_actor.actor_type,
    ) != (
        operation_actor.tenant_id,
        operation_actor.actor_id,
        operation_actor.actor_type,
    ) or run.request_context.content_ref != operation_context.content_ref:
        raise _recovery_error(
            "AGENT_RECOVERY_PROVENANCE_MISMATCH",
            "Skill receipt Run belongs to another actor or content version",
        )
    expected_identity = (
        event.session_id,
        event.turn_id,
        event.command_id,
        scope.session.world_id,
        event.skill_ref,
        event.expected_world_revision,
    )
    actual_identity = (
        run.session_id,
        run.turn_id,
        run.command_id,
        run.world_id,
        run.skill_ref,
        run.world_revision_before,
    )
    if actual_identity != expected_identity:
        raise _recovery_error(
            "AGENT_RECOVERY_RUN_IDENTITY_MISMATCH",
            "Skill receipt does not belong to the accepted event",
        )
    run_evidence = {item.evidence_id: item for item in run.evidence_refs}
    for item in event.evidence_refs:
        if run_evidence.get(item.evidence_id) != item:
            raise _recovery_error(
                "AGENT_RECOVERY_EVIDENCE_MISMATCH",
                "Skill receipt is missing immutable event Evidence",
            )

    summary = {
        "run_id": run.run_id,
        "task_success": run.task_success,
        "world_revision_before": run.world_revision_before,
        "world_revision_after": run.world_revision_after,
        "world_difference": run.world_difference,
        "evidence_ids": [item.evidence_id for item in run.evidence_refs],
    }
    call_suffix = hashlib.sha256(result.invocation_id.encode("utf-8")).hexdigest()[:16]
    tool_call = ToolCallRecord(
        execution_id=result.invocation_id,
        model_call_id=f"call_recovery_{call_suffix}",
        name="invoke_skill",
        arguments={"skill_id": "bound_skill", "arguments": result.arguments},
        result_summary=summary,
    )
    if run.task_success:
        message = (
            f"已从幂等运行回执恢复（Run {run.run_id}）：任务结果成功，"
            f"世界版本 {run.world_revision_before}→{run.world_revision_after}。"
        )
    else:
        message = (
            f"已从幂等运行回执恢复（Run {run.run_id}）：任务尚未完成，"
            f"世界版本 {run.world_revision_before}→{run.world_revision_after}。"
        )
    evidence = _merge_evidence(event.evidence_refs, run.evidence_refs)
    return AgentDecision(
        draft=DecisionDraft(
            role="xiaohutao",
            response_type="message",
            message=message,
            question=None,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        ),
        message_key="agent.skill.recovery",
        source="provider_fallback",
        degraded=True,
        fallback_reason="SIDE_EFFECT_RECEIPT_RECOVERED",
        provider="runtime",
        model="deterministic-recovery-v1",
        input_tokens=0,
        output_tokens=0,
        tool_calls=(tool_call,),
        evidence_refs=evidence,
        completed_at=completed_at,
        runtime_warnings=runtime_warnings,
    )


def _merge_evidence(*groups: Sequence[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    merged: dict[str, EvidenceRef] = {}
    for group in groups:
        for item in group:
            previous = merged.get(item.evidence_id)
            if previous is not None and previous != item:
                raise _recovery_error(
                    "AGENT_RECOVERY_EVIDENCE_COLLISION",
                    "same evidence_id carries different immutable metadata",
                )
            merged[item.evidence_id] = item
    if len(merged) > 64:
        raise _recovery_error(
            "AGENT_RECOVERY_EVIDENCE_LIMIT_EXCEEDED",
            "recovered feedback exceeds the immutable Evidence limit",
        )
    return tuple(merged.values())


def _recovery_error(code: str, message: str) -> AgentPersistenceError:
    return AgentPersistenceError(code, message)


__all__ = ["recover_skill_invocation_decision"]
