"""Bounded prompts built only from validated context and tool summaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from yaya_agent_contracts import LlmMessage

from .domain import FrozenObject, RoleId, TurnContext, thaw_value
from .evidence import alias_evidence_refs, build_evidence_aliases
from .model_output import ModelToolCall
from .pedagogy_policy import TeachingDirective
from .role_config import RoleConfig

_COMMON_RULES = """你正在参与一个面向编程初学者的农场游戏。
只使用本轮上下文、Evidence 和由 Runtime 返回的工具结果，不编造运行、世界变化、测试或学生经历。
一次只聚焦一个核心问题；AI 推断不能写成永久能力结论。
必须只输出 output_schema 允许的 JSON 对象，不要输出 Markdown 或解释文字。
工具调用只是请求；只有 Runtime 返回的工具结果才是事实。"""


class PromptBuilder:
    def initial_messages(
        self,
        config: RoleConfig,
        context: TurnContext,
        tool_definitions: tuple[FrozenObject, ...],
    ) -> tuple[LlmMessage, ...]:
        system = (
            f"{_COMMON_RULES}\n\n角色：{config.display_name}\n职责：{config.purpose}\n"
            f"角色规则：{config.prompt}\n当前提示等级：{context.hint_level}\n"
            "如需工具，返回 kind=tool_calls、decision=null；否则返回 kind=decision、tool_calls=[]。"
            "每个工具调用必须完整包含 call_id、name、arguments；call_id 必须以字母或数字开头且至少 8 个字符。"
            "不要使用 API 原生 tool_calls 字段，闭合对象必须直接放在消息 content 中。"
            "available_tools 是本轮完整且穷尽的工具集合，不得请求未列出的工具。"
        )
        if context.teaching_directive is not None:
            system += (
                "\nThe Runtime-owned TeachingDirective is immutable. Do not change its phase, "
                "target_concept, required_evidence_refs, allowed response types, or hint ceiling. "
                "Full-solution output is disabled."
            )
            if context.teaching_directive.patch_eligible:
                system += (
                    "\nThe student explicitly requested a Skill Patch. Return exactly one full "
                    "replacement_content for the current entrypoint plus a bounded rationale. "
                    "Do not output IDs, paths, revisions, hashes, Evidence metadata, Build/Run "
                    "identity, CAS data, or any write/build/activate/run instruction; Runtime "
                    "injects all authority and the student must separately accept it."
                )
            else:
                system += " Skill Patch output is not eligible for this turn."
            if (
                context.role in {"teaching_agent", "bug_agent"}
                and not context.teaching_directive.required_evidence_ids
            ):
                system += (
                    "\nThis directive has no required Evidence. Do not claim an observed run "
                    "or call a tool to invent missing run context. Ask one bounded diagnostic "
                    "question about target_concept and keep learner_inference null."
                )
        payload = {
            "turn_context": _context_payload(context),
            "available_tools": [thaw_value(item) for item in tool_definitions],
        }
        return (
            LlmMessage("system", system),
            LlmMessage("user", _json(payload)),
        )

    def after_validation_error(
        self,
        messages: tuple[LlmMessage, ...],
        *,
        role: RoleId,
        error_code: str,
        details: Mapping[str, object],
        final_only: bool,
        directive: TeachingDirective | None = None,
        required_evidence_aliases: tuple[str, ...] = (),
    ) -> tuple[LlmMessage, ...]:
        instruction = {
            "validation_failed": True,
            "error_code": error_code,
            "details": _redacted_details(details),
            "instruction": (
                "修正结构和语义，仅返回最终 kind=decision，不得再次调用工具。"
                "响应第一个字符必须是 {，最后一个非空白字符必须是 }；"
                "不得包含 Markdown 代码围栏、<think> 标签或 JSON 前后的解释。"
                if final_only
                else "修正结构和语义后重新返回闭合 JSON。"
            ),
        }
        if final_only:
            instruction["required_final_envelope_shape"] = _final_decision_shape(
                role, directive, required_evidence_aliases
            )
        return messages + (LlmMessage("user", _json(instruction)),)

    def after_tools(
        self,
        messages: tuple[LlmMessage, ...],
        calls: tuple[ModelToolCall, ...],
        results: tuple[FrozenObject, ...],
        *,
        role: RoleId,
        directive: TeachingDirective | None = None,
        required_evidence_aliases: tuple[str, ...] = (),
    ) -> tuple[LlmMessage, ...]:
        requested = {
            "kind": "tool_calls",
            "decision": None,
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": thaw_value(call.arguments),
                }
                for call in calls
            ],
        }
        updated = messages + (LlmMessage("assistant", _json(requested)),)
        for call, result in zip(calls, results, strict=True):
            updated += (
                LlmMessage(
                    "user",
                    _json(
                        {
                            "runtime_tool_result": True,
                            "call_id": call.call_id,
                            "tool": call.name,
                            "result_summary": _provider_safe_value(thaw_value(result)),
                        }
                    ),
                ),
            )
        return updated + (
            LlmMessage(
                "user",
                _json(
                    {
                        "instruction": (
                            "根据以上由 Runtime 验证的工具结果返回最终 kind=decision；"
                            "tool_calls 必须为空。严格保留下列对象的字段、固定值和类型；"
                            "只根据 Evidence 填写简短可核验的 message/question，以及"
                            "learner_inference 中有界的 score_delta、confidence 和 reason。"
                            "不得改变 concept 或 evidence_ids。响应第一个字符必须是 {，"
                            "最后一个非空白字符必须是 }；不得包含 Markdown 代码围栏、"
                            "<think> 标签或 JSON 前后的解释。"
                        ),
                        "required_final_envelope_shape": _final_decision_shape(
                            role, directive, required_evidence_aliases
                        ),
                    }
                ),
            ),
        )


def _final_decision_shape(
    role: RoleId,
    directive: TeachingDirective | None = None,
    required_evidence_aliases: tuple[str, ...] = (),
) -> dict[str, object]:
    response_type = "message" if directive is None else directive.allowed_response_types[0]
    learner_inference: object = None
    if (
        directive is not None
        and role in {"teaching_agent", "bug_agent", "book_agent"}
        and required_evidence_aliases
        and response_type != "skill_patch"
    ):
        learner_inference = {
            "concept": directive.target_concept,
            "score_delta": -0.1,
            "confidence": 0.8,
            "reason": "仅根据上述 Runtime Evidence 给出简短、可核验的推断理由。",
            "evidence_ids": list(required_evidence_aliases),
        }
    return {
        "kind": "decision",
        "decision": {
            "role": role,
            "response_type": response_type,
            "message": "根据 Runtime 工具结果给出简短、可核验的反馈。",
            "question": (
                "Ask one evidence-grounded question." if response_type == "question" else None
            ),
            "hint_level": (
                directive.hint_level if response_type == "hint" and directive is not None else None
            ),
            "learner_inference": learner_inference,
            "skill_patch": (
                {
                    "replacement_content": "full replacement source for current entrypoint",
                    "rationale": "Evidence-grounded reason for this one replacement.",
                }
                if response_type == "skill_patch"
                else None
            ),
            "requires_student_confirmation": response_type == "skill_patch",
        },
        "tool_calls": [],
    }


def _context_payload(context: TurnContext) -> dict[str, object]:
    event = context.event
    evidence_aliases, evidence_types = build_evidence_aliases(context)
    payload: dict[str, object] = {
        "event": {
            "event_type": event.event_type,
            "failure_count": event.failure_count,
            "failure_key": event.failure_key,
            "expected_world_revision": event.expected_world_revision,
            "evidence_refs": alias_evidence_refs(event.evidence_refs, evidence_aliases),
        },
        "task": {
            "title": context.task.title,
            "goal": context.task.goal,
            "story": context.task.story,
            "knowledge_points": context.task.knowledge_points,
        },
        "hint_level": context.hint_level,
        "evidence_catalog": [
            {"ref": alias, "type": evidence_types[evidence_id]}
            for evidence_id, alias in evidence_aliases.items()
        ],
    }
    directive = context.teaching_directive
    if directive is not None:
        payload["teaching_directive"] = {
            "phase": directive.phase.value,
            "target_concept": directive.target_concept,
            "hint_level": directive.hint_level,
            "allowed_response_types": directive.allowed_response_types,
            "patch_eligible": directive.patch_eligible,
            "full_solution_eligible": directive.full_solution_eligible,
            "required_evidence_refs": [
                evidence_aliases[item] for item in directive.required_evidence_ids
            ],
            "reason_codes": directive.reason_codes,
            "pedagogy_policy_version": directive.pedagogy_policy_version,
            "learner_revision": directive.learner_revision,
            "teaching_spec_version": directive.teaching_spec_version,
        }
    if context.world is not None:
        payload["world"] = {
            "revision": context.world.revision,
            "visible_state": _provider_safe_value(thaw_value(context.world.visible_state)),
        }
    if context.skill is not None:
        skill_payload: dict[str, object] = {
            "binding": "bound_skill",
            "entrypoint": context.skill.entrypoint,
            "parameter_schema": thaw_value(context.skill.parameter_schema),
        }
        if context.role in {"teaching_agent", "bug_agent"}:
            skill_payload["source_code"] = context.skill.source_code
        payload["skill"] = skill_payload
        if context.role == "xiaohutao" and event.event_type == "run_skill_requested":
            payload["required_first_tool"] = {
                "name": "invoke_skill",
                "skill_id": "bound_skill",
                "arguments_schema": thaw_value(context.skill.parameter_schema),
                "envelope": {
                    "kind": "tool_calls",
                    "decision": None,
                    "required_call_fields": ["call_id", "name", "arguments"],
                },
            }
    if context.patch_authority is not None:
        # Model sees only source and opaque aliases. All identity/path/hash/CAS
        # authority remains outside the provider-controlled envelope.
        payload["skill_patch_request"] = {
            "explicit_student_request": True,
            "operation": "UPSERT_FILE",
            "target": "CURRENT_ENTRYPOINT",
            "requires_student_confirmation": True,
            "auto_build": False,
            "auto_activate": False,
            "auto_run": False,
        }
    if context.available_skills:
        payload["available_skills"] = [
            {
                "binding": (
                    "bound_skill"
                    if context.skill is not None and item.ref == context.skill.ref
                    else f"available_skill_{index}"
                ),
                "parameter_schema": thaw_value(item.parameter_schema),
            }
            for index, item in enumerate(context.available_skills, start=1)
        ]
    if context.compile_result is not None:
        payload["compile_result"] = {
            "succeeded": context.compile_result.succeeded,
            "diagnostics": context.compile_result.diagnostics,
            "evidence_refs": alias_evidence_refs(
                context.compile_result.evidence_refs,
                evidence_aliases,
            ),
        }
    if context.run_result is not None:
        payload["run_result"] = _run_payload(context.run_result, evidence_aliases)
    if context.failure_history:
        payload["failure_history"] = [
            _run_payload(item, evidence_aliases) for item in context.failure_history
        ]
    if context.counterexamples:
        payload["counterexamples"] = [
            {
                "failure_key": item.failure_key,
                "title": item.title,
                "input": _provider_safe_value(thaw_value(item.input)),
                "observed": _provider_safe_value(thaw_value(item.observed)),
                "evidence_refs": alias_evidence_refs(item.evidence_refs, evidence_aliases),
            }
            for item in context.counterexamples
        ]
    if context.learner_profile is not None:
        payload["learner_profile"] = {
            "revision": context.learner_profile.revision,
            "competencies": thaw_value(context.learner_profile.competencies),
            "evidence_refs": alias_evidence_refs(
                context.learner_profile.evidence_refs,
                evidence_aliases,
            ),
        }
    if context.recent_messages:
        payload["recent_messages"] = [
            {"role": item.role, "message": item.message} for item in context.recent_messages
        ]
    if context.session_runs:
        payload["session_runs"] = [
            _run_payload(item, evidence_aliases) for item in context.session_runs
        ]
    if context.skill_history:
        payload["skill_history"] = [
            {
                "version_ordinal": index,
                "change_summary": item.change_summary,
            }
            for index, item in enumerate(context.skill_history, start=1)
        ]
    return payload


def _run_payload(run: object, evidence_aliases: Mapping[str, str]) -> dict[str, object]:
    from .domain import RunResultSnapshot

    if not isinstance(run, RunResultSnapshot):
        raise TypeError("run prompt value must be RunResultSnapshot")
    return {
        "task_success": run.task_success,
        "world_revision_before": run.world_revision_before,
        "world_revision_after": run.world_revision_after,
        "world_difference": _provider_safe_value(thaw_value(run.world_difference)),
        "failed_actions": _provider_safe_value(thaw_value(run.failed_actions)),
        "failure_key": run.failure_key,
        "evidence_refs": alias_evidence_refs(run.evidence_refs, evidence_aliases),
    }


def _provider_safe_value(value: object) -> object:
    """Remove correlation/resource identifiers before provider serialization."""

    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        safe: dict[str, object] = {}
        for raw_key, item in mapping.items():
            key = str(raw_key)
            lowered = key.lower()
            if (
                lowered.endswith("_id")
                or lowered.endswith("_ids")
                or lowered.endswith("_sha256")
                or lowered in {"sha256", "state_hash", "uri", "request_context"}
            ):
                continue
            safe[key] = _provider_safe_value(item)
        return safe
    if isinstance(value, list | tuple):
        items = cast(list[object] | tuple[object, ...], value)
        return [_provider_safe_value(item) for item in items]
    return value


def _redacted_details(details: Mapping[str, object]) -> dict[str, object]:
    allowed = {"object", "missing", "extra", "validation_error", "tool", "role", "path"}
    return {key: value for key, value in details.items() if key in allowed}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["PromptBuilder"]
