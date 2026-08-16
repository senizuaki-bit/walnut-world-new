"""Evidence-aware deterministic fallback decisions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from yaya_agent_contracts import EvidenceRef

from .domain import AgentDecision, DecisionDraft, RoleId, ToolCallRecord, TurnContext
from .evidence import collect_decision_evidence


def fallback_for(
    role: RoleId,
    context: TurnContext,
    *,
    reason: str,
    tool_calls: tuple[ToolCallRecord, ...],
    tool_evidence: Sequence[EvidenceRef],
    input_tokens: int,
    output_tokens: int,
    runtime_warnings: tuple[str, ...],
    completed_at: datetime,
) -> AgentDecision:
    if role == "world_agent":
        draft = DecisionDraft(
            role=role,
            response_type="message",
            message=context.task.goal,
            question=None,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        )
        message_key = "agent.world.fallback"
    elif role == "xiaohutao":
        invocation = next((item for item in tool_calls if item.name == "invoke_skill"), None)
        task_success = invocation.result_summary.get("task_success") if invocation else None
        run_id = invocation.result_summary.get("run_id") if invocation else None
        before = invocation.result_summary.get("world_revision_before") if invocation else None
        after = invocation.result_summary.get("world_revision_after") if invocation else None
        evidence_ids = invocation.result_summary.get("evidence_ids") if invocation else None
        evidence_id_values = (
            cast(tuple[object, ...], evidence_ids) if isinstance(evidence_ids, tuple) else ()
        )
        receipt_complete = (
            isinstance(run_id, str)
            and bool(run_id)
            and isinstance(before, int)
            and not isinstance(before, bool)
            and isinstance(after, int)
            and not isinstance(after, bool)
            and len(evidence_id_values) > 0
            and all(isinstance(item, str) and bool(item) for item in evidence_id_values)
        )
        if task_success is True and receipt_complete:
            message = f"实际运行已完成（Run {run_id}）：任务结果成功，世界版本 {before}→{after}。"
        elif task_success is False and receipt_complete:
            message = f"实际运行已完成（Run {run_id}）：任务尚未完成，世界版本 {before}→{after}。"
        else:
            message = "这次没有取得可验证的运行结果，我不会把它说成已经完成。"
        draft = DecisionDraft(
            role=role,
            response_type="message",
            message=message,
            question=None,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        )
        message_key = "agent.skill.fallback"
    elif role == "teaching_agent":
        if context.compile_result is not None:
            message = "代码还没有编译通过，请先查看第一条编译诊断。"
            question = "你能从编译器标出的第一处位置开始检查吗？"
        elif context.run_result is not None:
            message = f"运行 {context.run_result.run_id} 的记录显示任务目标还没有达到。"
            question = "哪一步的可观察结果和你的预期不同？"
        else:
            message = "我暂时没有足够的运行证据，只能先从当前代码和任务目标检查。"
            question = "你希望先检查哪一段代码？"
        draft = DecisionDraft(
            role=role,
            response_type="question",
            message=message,
            question=question,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        )
        message_key = "agent.teaching.fallback"
    elif role == "bug_agent":
        failure_key = context.event.failure_key or "当前边界条件"
        draft = DecisionDraft(
            role=role,
            response_type="question",
            message=f"同类失败记录已经复现：{failure_key}。我只引用现有运行证据。",
            question="这个边界输入到达关键条件时，程序的判断会成立吗？",
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        )
        message_key = "agent.bug.fallback"
    else:
        draft = DecisionDraft(
            role="book_agent",
            response_type="growth_summary",
            message="任务已经由运行和世界记录确认完成；本次成长细节暂时无法生成。",
            question=None,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        )
        message_key = "agent.book.fallback"

    evidence = _merge_evidence(collect_decision_evidence(context), tool_evidence)
    return AgentDecision(
        draft=draft,
        message_key=message_key,
        source="provider_fallback",
        degraded=True,
        fallback_reason=reason,
        provider="runtime",
        model="deterministic-fallback-v1",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_calls=tool_calls,
        evidence_refs=evidence,
        completed_at=completed_at,
        runtime_warnings=runtime_warnings,
        teaching_directive=context.teaching_directive,
    )


def _merge_evidence(*groups: Sequence[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    merged: dict[str, EvidenceRef] = {}
    for group in groups:
        for item in group:
            previous = merged.get(item.evidence_id)
            if previous is not None and previous != item:
                raise ValueError("same evidence_id cannot carry different immutable metadata")
            merged[item.evidence_id] = item
    return tuple(merged.values())


__all__ = ["fallback_for"]
