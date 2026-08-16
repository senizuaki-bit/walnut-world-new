"""Closed model-envelope schema and strict runtime parser."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from .domain import (
    DecisionDraft,
    FrozenObject,
    LearnerInference,
    ResponseType,
    RoleId,
    SkillPatchAuthority,
    SkillPatchProposal,
    freeze_object,
    thaw_value,
)
from .errors import InvalidAgentOutput
from .pedagogy_policy import TeachingDirective

_ROLE_IDS = ["world_agent", "xiaohutao", "teaching_agent", "bug_agent", "book_agent"]
_RESPONSE_TYPES = ["message", "question", "hint", "skill_patch", "growth_summary"]
_CALL_ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$"
_CALL_ID = re.compile(_CALL_ID_PATTERN)
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: FrozenObject


@dataclass(frozen=True, slots=True)
class ModelToolCallsEnvelope:
    calls: tuple[ModelToolCall, ...]


@dataclass(frozen=True, slots=True)
class ModelDecisionEnvelope:
    draft: DecisionDraft


type ModelEnvelope = ModelToolCallsEnvelope | ModelDecisionEnvelope


def build_model_output_schema(
    tool_definitions: tuple[FrozenObject, ...],
    *,
    max_tool_calls: int,
    role: RoleId | None = None,
    directive: TeachingDirective | None = None,
    required_evidence_aliases: tuple[str, ...] = (),
) -> dict[str, object]:
    decision = _decision_schema(
        role=role,
        directive=directive,
        required_evidence_aliases=required_evidence_aliases,
    )
    decision_envelope: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "decision", "tool_calls"],
        "properties": {
            "kind": {"type": "string", "const": "decision"},
            "decision": decision,
            "tool_calls": {
                "type": "array",
                "maxItems": 0,
                "items": {"type": "null"},
            },
        },
    }
    variants: list[dict[str, object]] = [decision_envelope]
    if tool_definitions and max_tool_calls > 0:
        call_variants: list[dict[str, object]] = []
        for definition in tool_definitions:
            name = definition["name"]
            input_schema = thaw_value(definition["input_schema"])
            call_variants.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["call_id", "name", "arguments"],
                    "properties": {
                        "call_id": {"type": "string", "pattern": _CALL_ID_PATTERN},
                        "name": {"type": "string", "const": name},
                        "arguments": input_schema,
                    },
                }
            )
        variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "decision", "tool_calls"],
                "properties": {
                    "kind": {"type": "string", "const": "tool_calls"},
                    "decision": {"type": "null"},
                    "tool_calls": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tool_calls,
                        "items": (
                            call_variants[0]
                            if len(call_variants) == 1
                            else {"oneOf": call_variants}
                        ),
                    },
                },
            }
        )
    return variants[0] if len(variants) == 1 else {"oneOf": variants}


def _decision_schema(
    *,
    role: RoleId | None = None,
    directive: TeachingDirective | None = None,
    required_evidence_aliases: tuple[str, ...] = (),
) -> dict[str, object]:
    nullable_string = {
        "oneOf": [
            {"type": "string", "minLength": 1, "maxLength": 1000},
            {"type": "null"},
        ]
    }
    nullable_hint: dict[str, object] = {
        "oneOf": [
            {"type": "integer", "minimum": 0, "maximum": 4},
            {"type": "null"},
        ]
    }
    evidence_ids_schema: dict[str, object] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 16,
        "uniqueItems": True,
        "items": {"type": "string", "pattern": "^evidence_[0-9]{3}$"},
    }
    if directive is not None:
        evidence_ids_schema = {
            "type": "array",
            "minItems": len(required_evidence_aliases),
            "maxItems": len(required_evidence_aliases),
            "prefixItems": [
                {"type": "string", "const": item} for item in required_evidence_aliases
            ],
            "items": False,
        }
    learner_object: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["concept", "score_delta", "confidence", "reason", "evidence_ids"],
        "properties": {
            "concept": (
                {"type": "string", "const": directive.target_concept}
                if directive is not None
                else {"type": "string", "minLength": 2, "maxLength": 128}
            ),
            "score_delta": {
                "type": "number",
                "minimum": -0.3,
                "maximum": 0.3,
                "multipleOf": 0.000001,
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "multipleOf": 0.000001,
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "evidence_ids": evidence_ids_schema,
        },
    }
    inference_allowed = role is None or (
        role in {"teaching_agent", "bug_agent", "book_agent"}
        and (directive is None or bool(required_evidence_aliases))
    )
    learner: dict[str, object]
    if not inference_allowed:
        learner = {"type": "null"}
    elif required_evidence_aliases:
        learner = learner_object
    else:
        learner = {"oneOf": [learner_object, {"type": "null"}]}
    patch: dict[str, object] = {"type": "null"}
    confirmation: dict[str, object] = {"type": "boolean", "const": False}
    if directive is not None and directive.patch_eligible:
        patch = {
            "type": "object",
            "additionalProperties": False,
            "required": ["replacement_content", "rationale"],
            "properties": {
                "replacement_content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1_048_576,
                },
                "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
        }
        learner = {"type": "null"}
        confirmation = {"type": "boolean", "const": True}
    required = [
        "role",
        "response_type",
        "message",
        "question",
        "hint_level",
        "learner_inference",
        "skill_patch",
        "requires_student_confirmation",
    ]
    properties: dict[str, object] = {
        "role": (
            {"type": "string", "const": role}
            if role is not None
            else {"type": "string", "enum": _ROLE_IDS}
        ),
        "response_type": (
            {"type": "string", "enum": list(directive.allowed_response_types)}
            if directive is not None
            else {"type": "string", "enum": _RESPONSE_TYPES}
        ),
        "message": {"type": "string", "minLength": 1, "maxLength": 4000},
        "question": nullable_string,
        "hint_level": nullable_hint,
        "learner_inference": learner,
        "skill_patch": patch,
        "requires_student_confirmation": confirmation,
    }
    base: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }
    if directive is None:
        return base

    variants: list[dict[str, object]] = []
    for response_type in directive.allowed_response_types:
        variant_properties = dict(properties)
        variant_properties["response_type"] = {
            "type": "string",
            "const": response_type,
        }
        if response_type == "question":
            variant_properties["question"] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
            }
            variant_properties["hint_level"] = {"type": "null"}
        elif response_type == "hint":
            variant_properties["question"] = {"type": "null"}
            variant_properties["hint_level"] = {
                "type": "integer",
                "const": directive.hint_level,
            }
        elif response_type == "skill_patch":
            variant_properties["question"] = {"type": "null"}
            variant_properties["hint_level"] = {"type": "integer", "const": 4}
            variant_properties["learner_inference"] = {"type": "null"}
            variant_properties["skill_patch"] = patch
            variant_properties["requires_student_confirmation"] = confirmation
        else:
            variant_properties["question"] = {"type": "null"}
            variant_properties["hint_level"] = {"type": "null"}
        variants.append({**base, "properties": variant_properties})
    return variants[0] if len(variants) == 1 else {"oneOf": variants}


def parse_model_envelope(
    output: Mapping[str, object],
    *,
    patch_authority: SkillPatchAuthority | None = None,
) -> ModelEnvelope:
    _exact_keys(output, {"kind", "decision", "tool_calls"}, "model envelope")
    kind = output["kind"]
    if kind == "decision":
        if output["tool_calls"] != [] and output["tool_calls"] != ():
            raise InvalidAgentOutput(
                "MODEL_ENVELOPE_CONTRADICTORY",
                "decision envelope must not contain tool calls",
            )
        decision = output["decision"]
        if not isinstance(decision, Mapping):
            raise InvalidAgentOutput(
                "MODEL_DECISION_MISSING",
                "decision envelope requires a decision object",
            )
        return ModelDecisionEnvelope(
            _parse_decision(
                cast(Mapping[str, object], decision),
                patch_authority=patch_authority,
            )
        )
    if kind == "tool_calls":
        if output["decision"] is not None:
            raise InvalidAgentOutput(
                "MODEL_ENVELOPE_CONTRADICTORY",
                "tool call envelope cannot also contain a decision",
            )
        raw_calls = output["tool_calls"]
        if isinstance(raw_calls, (str, bytes, bytearray)) or not isinstance(raw_calls, Sequence):
            raise InvalidAgentOutput("MODEL_TOOL_CALLS_INVALID", "tool_calls must be an array")
        if not raw_calls:
            raise InvalidAgentOutput("MODEL_TOOL_CALLS_EMPTY", "tool call envelope cannot be empty")
        calls: list[ModelToolCall] = []
        call_ids: set[str] = set()
        for index, raw_call in enumerate(cast(Sequence[object], raw_calls)):
            if not isinstance(raw_call, Mapping):
                raise InvalidAgentOutput(
                    "MODEL_TOOL_CALL_INVALID",
                    "each tool call must be an object",
                    {"index": index},
                )
            call = cast(Mapping[str, object], raw_call)
            _exact_keys(call, {"call_id", "name", "arguments"}, f"tool_calls[{index}]")
            call_id = call["call_id"]
            name = call["name"]
            arguments = call["arguments"]
            if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
                raise InvalidAgentOutput(
                    "MODEL_TOOL_CALL_ID_INVALID",
                    "tool call id has an invalid shape",
                    {"index": index},
                )
            if call_id in call_ids:
                raise InvalidAgentOutput(
                    "MODEL_TOOL_CALL_ID_DUPLICATE",
                    "tool call ids must be unique",
                    {"call_id": call_id},
                )
            if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
                raise InvalidAgentOutput(
                    "MODEL_TOOL_NAME_INVALID",
                    "tool name must be non-empty text",
                    {"index": index},
                )
            if not isinstance(arguments, Mapping):
                raise InvalidAgentOutput(
                    "MODEL_TOOL_ARGUMENTS_INVALID",
                    "tool arguments must be an object",
                    {"index": index},
                )
            call_ids.add(call_id)
            calls.append(
                ModelToolCall(
                    call_id,
                    name,
                    freeze_object(cast(Mapping[str, object], arguments), "tool arguments"),
                )
            )
        return ModelToolCallsEnvelope(tuple(calls))
    raise InvalidAgentOutput(
        "MODEL_ENVELOPE_KIND_INVALID",
        "model envelope kind must be decision or tool_calls",
    )


def _parse_decision(
    value: Mapping[str, object],
    *,
    patch_authority: SkillPatchAuthority | None,
) -> DecisionDraft:
    expected = {
        "role",
        "response_type",
        "message",
        "question",
        "hint_level",
        "learner_inference",
        "skill_patch",
        "requires_student_confirmation",
    }
    _exact_keys(value, expected, "decision")
    learner = _parse_learner(value["learner_inference"])
    patch = _parse_patch(value["skill_patch"], patch_authority=patch_authority)
    try:
        return DecisionDraft(
            role=cast(RoleId, value["role"]),
            response_type=cast(ResponseType, value["response_type"]),
            message=cast(str, value["message"]),
            question=cast(str | None, value["question"]),
            hint_level=cast(int | None, value["hint_level"]),
            learner_inference=learner,
            skill_patch=patch,
            requires_student_confirmation=cast(bool, value["requires_student_confirmation"]),
        )
    except (TypeError, ValueError) as error:
        raise InvalidAgentOutput(
            "MODEL_DECISION_INVALID",
            "model decision violates the closed domain contract",
            {"validation_error": str(error)[:300]},
        ) from error


def _parse_learner(value: object) -> LearnerInference | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidAgentOutput(
            "MODEL_LEARNER_INVALID", "learner_inference must be an object or null"
        )
    learner = cast(Mapping[str, object], value)
    _exact_keys(
        learner,
        {"concept", "score_delta", "confidence", "reason", "evidence_ids"},
        "learner_inference",
    )
    try:
        return LearnerInference(
            concept=cast(str, learner["concept"]),
            score_delta=cast(float, learner["score_delta"]),
            confidence=cast(float, learner["confidence"]),
            reason=cast(str, learner["reason"]),
            evidence_ids=tuple(cast(Sequence[str], learner["evidence_ids"])),
        )
    except (TypeError, ValueError) as error:
        raise InvalidAgentOutput(
            "MODEL_LEARNER_INVALID",
            "learner inference violates policy bounds",
            {"validation_error": str(error)[:300]},
        ) from error


def _parse_patch(
    value: object,
    *,
    patch_authority: SkillPatchAuthority | None,
) -> SkillPatchProposal | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidAgentOutput("MODEL_PATCH_INVALID", "skill_patch must be an object or null")
    if patch_authority is None:
        raise InvalidAgentOutput(
            "MODEL_PATCH_AUTHORITY_MISSING",
            "skill_patch output has no Runtime-owned Draft/Build/Run authority",
        )
    patch = cast(Mapping[str, object], value)
    _exact_keys(patch, {"replacement_content", "rationale"}, "skill_patch")
    try:
        return SkillPatchProposal.create(
            patch_authority,
            replacement_content=cast(str, patch["replacement_content"]),
            rationale=cast(str, patch["rationale"]),
        )
    except (TypeError, ValueError) as error:
        raise InvalidAgentOutput(
            "MODEL_PATCH_INVALID",
            "skill patch violates the internal proposal contract",
            {"validation_error": str(error)[:300]},
        ) from error


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise InvalidAgentOutput(
            "MODEL_OBJECT_KEYS_MISMATCH",
            f"{label} must use the exact declared fields",
            {
                "object": label,
                "missing": sorted(expected - actual),
                "extra": sorted(actual - expected),
            },
        )


__all__ = [
    "ModelDecisionEnvelope",
    "ModelEnvelope",
    "ModelToolCall",
    "ModelToolCallsEnvelope",
    "build_model_output_schema",
    "parse_model_envelope",
]
