"""Semantic validation that structural schemas cannot express."""

from __future__ import annotations

import re
from typing import cast

from yaya_agent_contracts import EvidenceType

from .domain import DecisionDraft, LearnerInference, ToolCallRecord, TurnContext
from .errors import InvalidAgentOutput
from .evidence import build_evidence_aliases, collect_decision_evidence
from .role_config import RoleConfig

_FALSE_SUCCESS_PHRASES = (
    "全部完成",
    "成功完成任务",
    "任务已经完成",
    "所有地块都完成",
)
_PERMANENT_JUDGMENT_PHRASES = (
    "永久不掌握",
    "永久掌握",
    "完全不会",
    "永远不会",
    "永不再犯",
    "再也不会犯",
    "不会再犯任何错误",
    "没有编程能力",
    "permanent mastery",
    "mastered forever",
    "never had a failed run",
    "never fail again",
)
_PERMANENT_JUDGMENT_PATTERNS = (
    re.compile(r"\bnever\s+(?:make|repeat)\b.{0,40}\b(?:mistake|error)\b", re.IGNORECASE),
    re.compile(r"\bwill\s+not\s+fail\s+again\b", re.IGNORECASE),
)
_ROLE_RESPONSES = {
    "world_agent": frozenset({"message"}),
    "xiaohutao": frozenset({"message"}),
    "teaching_agent": frozenset({"question", "hint", "skill_patch"}),
    "bug_agent": frozenset({"question"}),
    "book_agent": frozenset({"growth_summary"}),
}


def _contains_permanent_judgment(value: str) -> bool:
    normalized = value.casefold()
    return any(phrase.casefold() in normalized for phrase in _PERMANENT_JUDGMENT_PHRASES) or any(
        pattern.search(value) for pattern in _PERMANENT_JUDGMENT_PATTERNS
    )


def validate_decision(
    decision: DecisionDraft,
    config: RoleConfig,
    context: TurnContext,
    tool_calls: tuple[ToolCallRecord, ...],
) -> DecisionDraft:
    if decision.role != config.id or decision.role != context.role:
        raise InvalidAgentOutput(
            "ROLE_MISMATCH",
            "model decision role does not match deterministic routing",
            {"expected": context.role, "actual": decision.role},
        )
    directive = context.teaching_directive
    if context.role == "xiaohutao":
        if directive is not None:
            raise InvalidAgentOutput(
                "UNEXPECTED_TEACHING_DIRECTIVE",
                "xiaohutao execution receipts cannot carry a TeachingDirective",
            )
    elif directive is None:
        raise InvalidAgentOutput(
            "TEACHING_DIRECTIVE_MISSING",
            "directive-bearing role has no deterministic TeachingDirective",
        )
    if len(decision.message) > config.limits.max_message_chars:
        raise InvalidAgentOutput(
            "MESSAGE_TOO_LONG",
            "model message exceeds the selected role limit",
            {"maximum": config.limits.max_message_chars, "actual": len(decision.message)},
        )
    if decision.response_type not in _ROLE_RESPONSES[context.role]:
        raise InvalidAgentOutput(
            "RESPONSE_TYPE_ROLE_MISMATCH",
            "response_type is not allowed for the deterministic role",
            {"role": context.role, "response_type": decision.response_type},
        )
    if directive is not None and decision.response_type not in directive.allowed_response_types:
        raise InvalidAgentOutput(
            "RESPONSE_TYPE_DIRECTIVE_MISMATCH",
            "response_type exceeds the deterministic TeachingDirective",
            {
                "response_type": decision.response_type,
                "allowed": list(directive.allowed_response_types),
            },
        )
    if _contains_permanent_judgment(decision.message):
        raise InvalidAgentOutput(
            "PERMANENT_LEARNER_JUDGMENT",
            "feedback turns one observation into a permanent learner judgment",
        )
    if decision.response_type == "hint" and decision.hint_level != context.hint_level:
        raise InvalidAgentOutput(
            "HINT_LEVEL_MISMATCH",
            "structured hint level does not match the deterministic policy",
            {"expected": context.hint_level, "actual": decision.hint_level},
        )
    if decision.response_type not in {"hint", "skill_patch"} and decision.hint_level is not None:
        raise InvalidAgentOutput(
            "HINT_LEVEL_UNAUTHORIZED",
            "only an allowed hint response may carry the directive hint level",
        )
    if decision.skill_patch is not None:
        if directive is None or not directive.patch_eligible:
            raise InvalidAgentOutput(
                "PATCH_NOT_ELIGIBLE",
                "the deterministic TeachingDirective forbids Skill Patch",
            )
        if not config.limits.allow_skill_patch:
            raise InvalidAgentOutput(
                "PATCH_NOT_ALLOWED_FOR_ROLE",
                "selected role configuration forbids patches",
            )
        if context.hint_level != 4:
            raise InvalidAgentOutput(
                "PATCH_HINT_LEVEL_INVALID",
                "a patch is only allowed at deterministic hint level 4",
            )
        if (
            config.limits.require_confirmation_for_patch
            and not decision.requires_student_confirmation
        ):
            raise InvalidAgentOutput(
                "PATCH_REQUIRES_CONFIRMATION",
                "patch proposal did not require student confirmation",
            )
        if context.skill is None or context.patch_authority is None:
            raise InvalidAgentOutput(
                "PATCH_SOURCE_MISSING",
                "patch proposal has no runtime-owned Draft/Build/Run authority",
            )
        patch = decision.skill_patch
        if patch.target != context.patch_authority.target:
            raise InvalidAgentOutput(
                "PATCH_AUTHORITY_MISMATCH",
                "internal patch changed the exact current Draft authority",
            )
        if patch.request != context.patch_authority.request:
            raise InvalidAgentOutput(
                "PATCH_REQUEST_MISMATCH",
                "internal patch changed the explicit UI-action request authority",
            )
        if patch.failed != context.patch_authority.failed:
            raise InvalidAgentOutput(
                "PATCH_EVIDENCE_MISMATCH",
                "internal patch changed the exact failed Build/Run/Evidence authority",
            )
        if (
            patch.operation.operation_type != "UPSERT_FILE"
            or patch.operation.path != context.skill.entrypoint
            or patch.operation.previous_content_sha256 != context.skill.source_sha256
        ):
            raise InvalidAgentOutput(
                "PATCH_OPERATION_INVALID",
                "internal patch must be one full UPSERT of the exact current entrypoint",
            )

    current_learning_evidence = {
        ref.evidence_id
        for ref in collect_decision_evidence(context)
        if ref.evidence_type
        in {
            EvidenceType.ACTION_LOG,
            EvidenceType.SANDBOX_LOG,
            EvidenceType.TEST_REPORT,
            EvidenceType.WORLD_COMMIT,
        }
    }
    inference = decision.learner_inference
    if (
        inference is None
        and decision.role in {"teaching_agent", "bug_agent", "book_agent"}
        and directive is not None
        and bool(directive.required_evidence_ids)
        and set(directive.required_evidence_ids).issubset(current_learning_evidence)
        and decision.response_type != "skill_patch"
    ):
        raise InvalidAgentOutput(
            "LEARNER_INFERENCE_REQUIRED",
            "current directive Evidence requires an explicit learner inference",
        )
    if inference is not None:
        if decision.response_type == "skill_patch":
            raise InvalidAgentOutput(
                "PATCH_LEARNER_INFERENCE_FORBIDDEN",
                "a Patch proposal cannot claim a learner outcome before acceptance and rerun",
            )
        if decision.role not in {"teaching_agent", "bug_agent", "book_agent"}:
            raise InvalidAgentOutput(
                "LEARNER_INFERENCE_NOT_ALLOWED",
                "this role cannot propose a learner inference",
            )
        if _contains_permanent_judgment(inference.reason):
            raise InvalidAgentOutput(
                "PERMANENT_LEARNER_JUDGMENT",
                "learner inference reason turns one observation into a permanent judgment",
            )
        if inference.concept not in context.task.knowledge_points:
            raise InvalidAgentOutput(
                "UNKNOWN_CONCEPT",
                "learner inference is outside the current task knowledge points",
                {"concept": inference.concept},
            )
        if directive is None or inference.concept != directive.target_concept:
            raise InvalidAgentOutput(
                "LEARNER_INFERENCE_CONCEPT_DIRECTIVE_MISMATCH",
                "learner inference changed the deterministic target concept",
                {
                    "expected": None if directive is None else directive.target_concept,
                    "actual": inference.concept,
                },
            )
        aliases, _ = build_evidence_aliases(context)
        by_alias = {alias: evidence_id for evidence_id, alias in aliases.items()}
        try:
            resolved_evidence = tuple(by_alias[item] for item in inference.evidence_ids)
        except KeyError as error:
            raise InvalidAgentOutput(
                "LEARNER_INFERENCE_EVIDENCE_MISMATCH",
                "learner inference references Evidence outside the validated context",
                {"evidence_alias": str(error.args[0])},
            ) from error
        if not set(resolved_evidence).issubset(current_learning_evidence):
            raise InvalidAgentOutput(
                "LEARNER_INFERENCE_WITHOUT_RUN_EVIDENCE",
                "learner inference must reference action, Sandbox, test or World evidence",
            )
        required = set(directive.required_evidence_ids)
        if set(resolved_evidence) != required:
            raise InvalidAgentOutput(
                "LEARNER_INFERENCE_EVIDENCE_DIRECTIVE_MISMATCH",
                "learner inference changed the deterministic Evidence set",
                {
                    "expected_count": len(required),
                    "actual_count": len(resolved_evidence),
                },
            )
        decision = _replace_inference(
            decision,
            LearnerInference(
                concept=inference.concept,
                score_delta=inference.score_delta,
                confidence=inference.confidence,
                reason=inference.reason,
                evidence_ids=resolved_evidence,
            ),
        )

    invoke_records = tuple(item for item in tool_calls if item.name == "invoke_skill")
    if context.role == "xiaohutao":
        if len(invoke_records) != 1:
            raise InvalidAgentOutput(
                "SKILL_INVOCATION_REQUIRED",
                "xiaohutao must execute exactly one certified skill before responding",
                {"actual": len(invoke_records)},
            )
        task_success = invoke_records[0].result_summary.get("task_success")
        if not isinstance(task_success, bool):
            raise InvalidAgentOutput(
                "SKILL_RESULT_INVALID",
                "invoke_skill summary lacks an objective task_success value",
            )
        if not task_success and any(
            phrase in decision.message for phrase in _FALSE_SUCCESS_PHRASES
        ):
            raise InvalidAgentOutput(
                "FALSE_SUCCESS_CLAIM",
                "message claims success while the canonical run says the task failed",
            )
        if decision.response_type != "message" or decision.learner_inference is not None:
            raise InvalidAgentOutput(
                "SKILL_FEEDBACK_SHAPE_INVALID",
                "xiaohutao must return one plain execution receipt message",
            )
        decision = _canonical_skill_receipt(decision, invoke_records[0])
    elif invoke_records:
        raise InvalidAgentOutput(
            "SKILL_INVOCATION_ROLE_MISMATCH",
            "only xiaohutao may execute invoke_skill",
        )

    if context.event.event_type == "hint_requested" and any(
        phrase in decision.message
        or (decision.question is not None and phrase in decision.question)
        for phrase in _FALSE_SUCCESS_PHRASES
    ):
        # A hint carries no Run, so it has no authority to settle the outcome
        # either way.  This replaces what the deterministic copy used to say
        # for it ("I will not infer success or failure").
        raise InvalidAgentOutput(
            "HINT_CLAIMS_OUTCOME",
            "a hint has no bound Run and cannot claim the task succeeded",
        )
    run = context.run_result
    if run is not None and not run.task_success:
        if any(phrase in decision.message for phrase in _FALSE_SUCCESS_PHRASES):
            raise InvalidAgentOutput(
                "FALSE_SUCCESS_CLAIM",
                "message claims success while the bound run says the task failed",
            )
        if context.role in {"teaching_agent", "bug_agent"}:
            decision = _replace_message(
                decision,
                f"规范运行记录确认任务尚未完成；失败类型为 {run.failure_key}。",
            )
    if context.compile_result is not None and not context.compile_result.succeeded:
        first_diagnostic = context.compile_result.diagnostics[0][:160]
        decision = _replace_message(
            decision,
            f"规范编译记录确认代码未通过；第一条诊断是：{first_diagnostic}",
        )
    if context.role == "bug_agent" and len(context.failure_history) < 3:
        raise InvalidAgentOutput(
            "BUG_WITHOUT_REPRODUCIBLE_EVIDENCE",
            "bug role requires three same-class failures",
        )
    if context.role == "book_agent" and (run is None or not run.task_success):
        raise InvalidAgentOutput(
            "GROWTH_SUMMARY_WITHOUT_COMPLETION",
            "book role requires an objectively successful completion run",
        )
    if decision.response_type == "skill_patch":
        return decision
    if context.role == "world_agent":
        decision = _canonical_world_copy(decision, context, config.limits.max_message_chars)
    elif context.role == "teaching_agent":
        decision = _canonical_teaching_copy(decision, context, config.limits.max_message_chars)
    elif context.role == "bug_agent":
        decision = _canonical_bug_copy(decision, context, config.limits.max_message_chars)
    elif context.role == "book_agent":
        decision = _canonical_book_copy(decision, context, config.limits.max_message_chars)
    return decision


def _replace_message(decision: DecisionDraft, message: str) -> DecisionDraft:
    return DecisionDraft(
        role=decision.role,
        response_type=decision.response_type,
        message=message,
        question=decision.question,
        hint_level=decision.hint_level,
        learner_inference=decision.learner_inference,
        skill_patch=decision.skill_patch,
        requires_student_confirmation=decision.requires_student_confirmation,
    )


def _replace_public_copy(
    decision: DecisionDraft,
    *,
    message: str,
    question: str | None,
) -> DecisionDraft:
    return DecisionDraft(
        role=decision.role,
        response_type=decision.response_type,
        message=message,
        question=question,
        hint_level=decision.hint_level,
        learner_inference=decision.learner_inference,
        skill_patch=decision.skill_patch,
        requires_student_confirmation=decision.requires_student_confirmation,
    )


def _canonical_world_copy(
    decision: DecisionDraft,
    context: TurnContext,
    maximum: int,
) -> DecisionDraft:
    story = f"{context.task.story.strip()} " if context.task.story.strip() else ""
    message = _bounded(
        f"{story}新任务“{context.task.title}”。可观察目标：{context.task.goal}",
        maximum,
    )
    return _replace_public_copy(decision, message=message, question=None)


def _canonical_teaching_copy(
    decision: DecisionDraft,
    context: TurnContext,
    maximum: int,
) -> DecisionDraft:
    if context.event.event_type == "hint_requested":
        # A hint is the one teaching turn with no compile or run result to
        # restate, so the deterministic copy below would collapse every hint to
        # the same content-free sentence and discard the only thing the student
        # pressed the button for: the model's reading of their current source.
        #
        # Keeping the prose is not trusting it blindly.  By this point it has
        # already passed the role/response_type/hint_level checks, the length
        # limit, the permanent-judgment ban and the output schema, and
        # `validate_decision` additionally forbids a hint from claiming the task
        # succeeded, because a hint has no authoritative Run behind it.
        del maximum
        return decision
    if context.compile_result is not None:
        diagnostic = context.compile_result.diagnostics[0][:160]
        fact = f"规范编译记录确认代码未通过；第一条诊断是：{diagnostic}"
        question = "你能从编译器标出的第一处位置开始检查吗？"
    elif context.run_result is not None:
        failure_key = context.run_result.failure_key or "未分类运行失败"
        fact = f"规范运行记录确认任务尚未完成；失败类型为 {failure_key}。"
        question = "哪一步的可观察结果和你的预期不同？"
    else:
        fact = "本轮没有绑定编译或运行结果，因此我不会推断代码已经成功或失败。"
        question = "你准备先对照任务目标检查哪一段代码？"

    if decision.response_type == "question":
        return _replace_public_copy(
            decision,
            message=_bounded(fact, maximum),
            question=question,
        )

    level = decision.hint_level
    if level is None:
        raise AssertionError("validated teaching hint must carry hint_level")
    entrypoint = context.skill.entrypoint if context.skill is not None else "当前入口文件"
    concept = context.task.knowledge_points[0] if context.task.knowledge_points else "循环边界"
    hints: dict[int, str] = {
        0: "先只比较可观察结果与任务目标，不急着改代码。",
        1: f"先检查 {entrypoint} 中控制循环或条件的代码区域。",
        2: f"重点核对 {concept} 与输入规模、起止边界之间的关系。",
        3: "尝试只调整控制迭代范围的局部条件，再用最小和最大边界重新运行。",
    }
    hint = hints.get(level, "先从一个可验证的局部条件开始检查。")
    return _replace_public_copy(
        decision,
        message=_bounded(f"{fact}{hint}", maximum),
        question=None,
    )


def _canonical_bug_copy(
    decision: DecisionDraft,
    context: TurnContext,
    maximum: int,
) -> DecisionDraft:
    failure_key = context.event.failure_key or "当前边界条件"
    if context.counterexamples:
        evidence = f"已验证反例：{context.counterexamples[0].title}。"
    else:
        evidence = "当前没有额外反例，只使用同类失败 Run。"
    message = _bounded(
        f"同类失败已连续复现 {len(context.failure_history)} 次；失败类型为 {failure_key}。{evidence}",
        maximum,
    )
    question = "当边界输入到达关键条件时，循环或判断是否仍覆盖完整目标范围？"
    return _replace_public_copy(decision, message=message, question=question)


def _canonical_book_copy(
    decision: DecisionDraft,
    context: TurnContext,
    maximum: int,
) -> DecisionDraft:
    attempts = len(context.session_runs)
    failures = sum(not item.task_success for item in context.session_runs)
    versions = len(context.skill_history)
    message = _bounded(
        (
            f"规范运行和世界提交已确认“{context.task.title}”完成。"
            f"本 Session 共记录 {attempts} 次运行，其中 {failures} 次尚未完成，"
            f"并使用了 {versions} 个已记录 Skill 版本。"
            "具体进步：你让已认证 Skill 达成了当前可观察目标。"
            "可迁移问题：下次输入规模改变时，你会怎样先验证循环或条件边界？"
        ),
        maximum,
    )
    return _replace_public_copy(decision, message=message, question=None)


def _bounded(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    if maximum <= 1:
        return value[:maximum]
    return f"{value[: maximum - 1]}…"


def _replace_inference(
    decision: DecisionDraft,
    inference: LearnerInference,
) -> DecisionDraft:
    return DecisionDraft(
        role=decision.role,
        response_type=decision.response_type,
        message=decision.message,
        question=decision.question,
        hint_level=decision.hint_level,
        learner_inference=inference,
        skill_patch=decision.skill_patch,
        requires_student_confirmation=decision.requires_student_confirmation,
    )


def _canonical_skill_receipt(
    decision: DecisionDraft,
    invocation: ToolCallRecord,
) -> DecisionDraft:
    """Replace free-form execution claims with facts from the trusted Run receipt."""

    summary = invocation.result_summary
    run_id = summary.get("run_id")
    task_success = summary.get("task_success")
    revision_before = summary.get("world_revision_before")
    revision_after = summary.get("world_revision_after")
    evidence_ids = summary.get("evidence_ids")
    evidence_id_values = (
        cast(tuple[object, ...], evidence_ids) if isinstance(evidence_ids, tuple) else ()
    )
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(task_success, bool)
        or isinstance(revision_before, bool)
        or not isinstance(revision_before, int)
        or isinstance(revision_after, bool)
        or not isinstance(revision_after, int)
        or not evidence_id_values
        or any(not isinstance(item, str) or not item for item in evidence_id_values)
    ):
        raise InvalidAgentOutput(
            "SKILL_RESULT_INVALID",
            "invoke_skill summary lacks a complete canonical Run receipt",
        )
    if task_success:
        message = (
            f"实际运行已完成（Run {run_id}）：任务结果成功，"
            f"世界版本 {revision_before}→{revision_after}。"
        )
    else:
        message = (
            f"实际运行已完成（Run {run_id}）：任务尚未完成，"
            f"世界版本 {revision_before}→{revision_after}；请根据本次 Evidence 调整代码。"
        )
    return DecisionDraft(
        role=decision.role,
        response_type="message",
        message=message,
        question=None,
        hint_level=None,
        learner_inference=None,
        skill_patch=None,
        requires_student_confirmation=False,
    )


__all__ = ["validate_decision"]
