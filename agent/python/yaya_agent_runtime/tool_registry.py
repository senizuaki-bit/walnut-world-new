"""Closed-schema, role-authorized Agent tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from yaya_agent_contracts import EvidenceRef, EvidenceType, OperationContext

from .domain import (
    AgentTraceEvent,
    FrozenObject,
    RoleId,
    RunResultSnapshot,
    SkillInvocationRequest,
    ToolCallRecord,
    ToolResult,
    TurnContext,
    freeze_object,
    skill_invocation_request_sha256,
    thaw_value,
)
from .errors import (
    AgentConfigurationError,
    AgentDependencyError,
    AgentToolAuthorizationError,
    AgentToolError,
    AgentToolExecutionError,
)
from .ports import AgentTracePort, SkillInvocationPort
from .schema_validation import validate_instance, validate_schema_definition

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_IDENTIFIER_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$"
_BOUND_SKILL_ALIAS = "bound_skill"
type ToolSchemaFactory = Callable[[TurnContext], Mapping[str, object]]
type ToolAvailability = Callable[[TurnContext], bool]


def _always_available(_: TurnContext) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    execution_id: str
    model_call_id: str
    ordinal: int


class ToolHandler(Protocol):
    async def __call__(
        self,
        arguments: FrozenObject,
        turn_context: TurnContext,
        execution: ToolExecutionContext,
        operation_context: OperationContext,
    ) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class AgentTool:
    name: str
    description: str
    schema_factory: ToolSchemaFactory
    allowed_roles: frozenset[RoleId]
    handler: ToolHandler
    is_available: ToolAvailability = _always_available

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise AgentConfigurationError(
                "TOOL_NAME_INVALID",
                "tool name is invalid",
                {"tool": self.name},
            )
        if not 1 <= len(self.description) <= 1000:
            raise AgentConfigurationError(
                "TOOL_DESCRIPTION_INVALID",
                "tool description length must be between 1 and 1000",
                {"tool": self.name},
            )
        if not self.allowed_roles:
            raise AgentConfigurationError(
                "TOOL_ROLES_EMPTY",
                "every tool must allow at least one role",
                {"tool": self.name},
            )
        if not callable(self.is_available):
            raise AgentConfigurationError(
                "TOOL_AVAILABILITY_INVALID",
                "tool availability predicate must be callable",
                {"tool": self.name},
            )


class ToolRegistry:
    def __init__(self, trace: AgentTracePort) -> None:
        self._trace = trace
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise AgentConfigurationError(
                "TOOL_ALREADY_REGISTERED",
                "tool names must be unique",
                {"tool": tool.name},
            )
        self._tools[tool.name] = tool

    def model_definitions(
        self,
        role: RoleId,
        allowed_names: tuple[str, ...],
        context: TurnContext,
    ) -> tuple[FrozenObject, ...]:
        definitions: list[FrozenObject] = []
        for name in allowed_names:
            tool = self._get_configured_tool(name, role)
            if not tool.is_available(context):
                continue
            schema = tool.schema_factory(context)
            if not isinstance(schema, Mapping):
                raise AgentConfigurationError(
                    "TOOL_SCHEMA_INVALID",
                    "schema_factory must return an object",
                    {"tool": name},
                )
            schema_value = schema
            validate_schema_definition(schema_value)
            definitions.append(
                freeze_object(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": schema_value,
                    },
                    f"tool definition {name}",
                )
            )
        return tuple(definitions)

    def validate_call(
        self,
        *,
        role: RoleId,
        allowed_names: tuple[str, ...],
        name: str,
        arguments: Mapping[str, object],
        turn_context: TurnContext,
    ) -> FrozenObject:
        """Validate one model call without executing it or recording success.

        SharedAgentRuntime uses this to preflight the complete batch before a
        side-effect tool can run.  ``execute`` repeats the check at the actual
        boundary so callers cannot bypass it.
        """

        if name not in allowed_names:
            raise AgentToolAuthorizationError(
                "TOOL_NOT_ALLOWED_BY_ROLE_CONFIG",
                "tool is outside the selected role configuration",
                {"tool": name, "role": role},
            )
        tool = self._get_configured_tool(name, role)
        if not tool.is_available(turn_context):
            raise AgentToolAuthorizationError(
                "TOOL_UNAVAILABLE_FOR_CONTEXT",
                "tool is unavailable for the current validated context",
                {"tool": name, "role": role},
            )
        schema = tool.schema_factory(turn_context)
        if not isinstance(schema, Mapping):
            raise AgentConfigurationError(
                "TOOL_SCHEMA_INVALID",
                "schema_factory must return an object",
                {"tool": name},
            )
        validate_schema_definition(schema)
        validate_instance(arguments, schema)
        return freeze_object(arguments, "tool arguments")

    async def execute(
        self,
        *,
        role: RoleId,
        allowed_names: tuple[str, ...],
        model_call_id: str,
        ordinal: int,
        name: str,
        arguments: Mapping[str, object],
        turn_context: TurnContext,
        operation_context: OperationContext,
    ) -> tuple[ToolCallRecord, ToolResult, tuple[str, ...]]:
        execution_id = _execution_id(
            operation_context.command_id,
            turn_context.event.turn_id,
            ordinal,
            name,
        )
        execution = ToolExecutionContext(execution_id, model_call_id, ordinal)
        try:
            frozen_arguments = self.validate_call(
                role=role,
                allowed_names=allowed_names,
                name=name,
                arguments=arguments,
                turn_context=turn_context,
            )
            tool = self._get_configured_tool(name, role)
        except AgentToolError as error:
            warnings: list[str] = []
            try:
                await asyncio.wait_for(
                    self._trace.record(
                        AgentTraceEvent(
                            "agent.tool.rejected",
                            turn_context.event.turn_id,
                            role,
                            {
                                "execution_id": execution_id,
                                "tool": name,
                                "error_code": error.code,
                            },
                        ),
                        operation_context,
                    ),
                    timeout=1.0,
                )
            except Exception:
                warnings.append("TRACE_TOOL_REJECTED_WRITE_FAILED")
            if warnings:
                raise _with_runtime_warnings(error, warnings) from error
            raise

        warnings = []
        try:
            await asyncio.wait_for(
                self._trace.record(
                    AgentTraceEvent(
                        "agent.tool.started",
                        turn_context.event.turn_id,
                        role,
                        {"execution_id": execution_id, "tool": name, "ordinal": ordinal},
                    ),
                    operation_context,
                ),
                timeout=1.0,
            )
        except Exception:
            warnings.append("TRACE_TOOL_STARTED_WRITE_FAILED")
        try:
            result = await tool.handler(
                frozen_arguments,
                turn_context,
                execution,
                operation_context,
            )
            if not isinstance(result, ToolResult):
                raise AgentToolExecutionError(
                    "TOOL_RESULT_INVALID",
                    "tool handler returned a value outside ToolResult",
                    {"tool": name, "actual_type": type(result).__name__},
                )
            _enforce_result_size(result, name)
        except AgentToolError as error:
            warning = await self._record_failure(
                error, name, execution_id, turn_context, role, operation_context
            )
            if warning is not None:
                warnings.append(warning)
            if warnings:
                raise _with_runtime_warnings(error, warnings) from error
            raise
        except (AgentDependencyError, TimeoutError, ConnectionError) as error:
            converted = AgentToolExecutionError(
                "TOOL_DEPENDENCY_FAILED",
                "tool dependency did not produce a trustworthy result",
                {"tool": name, "cause_code": getattr(error, "code", type(error).__name__)},
            )
            warning = await self._record_failure(
                converted, name, execution_id, turn_context, role, operation_context
            )
            if warning is not None:
                warnings.append(warning)
            if warnings:
                converted = _with_runtime_warnings(converted, warnings)
            raise converted from error
        except Exception as error:
            converted = AgentToolExecutionError(
                "TOOL_HANDLER_FAILED",
                "tool handler raised an unexpected error",
                {"tool": name, "exception_type": type(error).__name__},
            )
            warning = await self._record_failure(
                converted, name, execution_id, turn_context, role, operation_context
            )
            if warning is not None:
                warnings.append(warning)
            if warnings:
                converted = _with_runtime_warnings(converted, warnings)
            raise converted from error

        record = ToolCallRecord(
            execution_id=execution_id,
            model_call_id=model_call_id,
            name=name,
            arguments=frozen_arguments,
            result_summary=result.summary,
        )
        try:
            await asyncio.wait_for(
                self._trace.record(
                    AgentTraceEvent(
                        "agent.tool.succeeded",
                        turn_context.event.turn_id,
                        role,
                        {
                            "execution_id": execution_id,
                            "tool": name,
                            "evidence_count": len(result.evidence_refs),
                        },
                    ),
                    operation_context,
                ),
                timeout=1.0,
            )
        except Exception:
            # The application tool may already have committed World/Evidence.
            # Preserve that fact and surface the observability loss in the
            # durable AgentDecision instead of throwing it away.
            warnings.append("TRACE_TOOL_SUCCEEDED_WRITE_FAILED")
        return record, result, tuple(warnings)

    def _get_configured_tool(self, name: str, role: RoleId) -> AgentTool:
        try:
            tool = self._tools[name]
        except KeyError as error:
            raise AgentConfigurationError(
                "TOOL_NOT_REGISTERED",
                "role configuration references an unregistered tool",
                {"tool": name, "role": role},
            ) from error
        if role not in tool.allowed_roles:
            raise AgentToolAuthorizationError(
                "TOOL_ROLE_FORBIDDEN",
                "tool definition does not authorize the selected role",
                {"tool": name, "role": role},
            )
        return tool

    async def _record_failure(
        self,
        error: AgentToolError,
        name: str,
        execution_id: str,
        context: TurnContext,
        role: RoleId,
        operation_context: OperationContext,
    ) -> str | None:
        try:
            await asyncio.wait_for(
                self._trace.record(
                    AgentTraceEvent(
                        "agent.tool.failed",
                        context.event.turn_id,
                        role,
                        {"execution_id": execution_id, "tool": name, "error_code": error.code},
                    ),
                    operation_context,
                ),
                timeout=1.0,
            )
        except Exception:
            return "TRACE_TOOL_FAILED_WRITE_FAILED"
        return None


def _with_runtime_warnings(
    error: AgentToolError,
    warnings: Sequence[str],
) -> AgentToolExecutionError:
    return AgentToolExecutionError(
        error.code,
        str(error),
        {**dict(error.details), "runtime_warnings": tuple(dict.fromkeys(warnings))},
    )


def _execution_id(command_id: str, turn_id: str, ordinal: int, tool_name: str) -> str:
    # A side effect keeps the same idempotency identity even if a provider
    # reorders read-only tools on an at-least-once worker retry.
    if tool_name == "invoke_skill":
        identity = f"{command_id}:{turn_id}:side-effect:{tool_name}:v1"
    else:
        identity = f"{command_id}:{turn_id}:read:{ordinal}:{tool_name}:v1"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"toolexec_{digest}"


def side_effect_execution_id(command_id: str, turn_id: str) -> str:
    """Stable receipt identity shared by Runtime execution and Hub recovery."""

    return _execution_id(command_id, turn_id, 1, "invoke_skill")


def _enforce_result_size(result: ToolResult, tool_name: str) -> None:
    value_bytes = len(
        json.dumps(thaw_value(result.value), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    summary_bytes = len(
        json.dumps(thaw_value(result.summary), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    if value_bytes > 1_048_576 or summary_bytes > 65_536:
        raise AgentToolExecutionError(
            "TOOL_RESULT_TOO_LARGE",
            "tool result exceeded the bounded runtime envelope",
            {"tool": tool_name, "value_bytes": value_bytes, "summary_bytes": summary_bytes},
        )


class FunctionToolHandler:
    def __init__(
        self,
        function: Callable[
            [FrozenObject, TurnContext, ToolExecutionContext, OperationContext],
            Awaitable[ToolResult],
        ],
    ) -> None:
        self._function = function

    async def __call__(
        self,
        arguments: FrozenObject,
        turn_context: TurnContext,
        execution: ToolExecutionContext,
        operation_context: OperationContext,
    ) -> ToolResult:
        return await self._function(arguments, turn_context, execution, operation_context)


class InvokeSkillToolHandler:
    def __init__(self, invocations: SkillInvocationPort) -> None:
        self._invocations = invocations

    async def __call__(
        self,
        arguments: FrozenObject,
        turn_context: TurnContext,
        execution: ToolExecutionContext,
        operation_context: OperationContext,
    ) -> ToolResult:
        skill = turn_context.skill
        world = turn_context.world
        if skill is None or world is None:
            raise AgentToolExecutionError(
                "TOOL_CONTEXT_INCOMPLETE",
                "invoke_skill requires a bound skill and canonical world summary",
            )
        skill_id = arguments["skill_id"]
        invocation_arguments = arguments["arguments"]
        if skill_id != _BOUND_SKILL_ALIAS or not isinstance(invocation_arguments, Mapping):
            raise AgentToolExecutionError(
                "TOOL_SKILL_BINDING_MISMATCH",
                "invoke_skill arguments do not match the bound certified skill",
            )
        invocation_arguments_value = cast(Mapping[str, object], invocation_arguments)
        request_sha256 = skill_invocation_request_sha256(
            tenant_id=operation_context.actor.tenant_id,
            invocation_id=execution.execution_id,
            session_id=turn_context.event.session_id,
            turn_id=turn_context.event.turn_id,
            command_id=turn_context.event.command_id,
            world_id=turn_context.session.world_id,
            expected_world_revision=turn_context.event.expected_world_revision,
            skill_ref=skill.ref,
            arguments=invocation_arguments_value,
        )
        request = SkillInvocationRequest(
            invocation_id=execution.execution_id,
            tenant_id=operation_context.actor.tenant_id,
            session_id=turn_context.event.session_id,
            turn_id=turn_context.event.turn_id,
            command_id=turn_context.event.command_id,
            world_id=turn_context.session.world_id,
            expected_world_revision=turn_context.event.expected_world_revision,
            skill_ref=skill.ref,
            arguments=invocation_arguments_value,
            request_sha256=request_sha256,
        )
        result = await self._invocations.invoke(request, operation_context)
        if result.invocation_id != request.invocation_id:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_ID_MISMATCH",
                "SkillInvocationPort returned a different idempotency identity",
            )
        if result.tenant_id != request.tenant_id:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_TENANT_MISMATCH",
                "SkillInvocationPort returned a receipt from another tenant",
            )
        if result.request_sha256 != request.request_sha256:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_REQUEST_MISMATCH",
                "SkillInvocationPort returned a receipt for different invocation bytes",
            )
        if result.arguments != request.arguments:
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_ARGUMENTS_MISMATCH",
                "SkillInvocationPort returned a receipt for different invocation arguments",
            )
        run = result.run
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
            raise AgentToolExecutionError(
                "TOOL_RUN_PROVENANCE_MISMATCH",
                "SkillInvocationPort returned a run authorized for another actor or content version",
            )
        expected_run_identity = (
            request.session_id,
            request.turn_id,
            request.command_id,
            request.world_id,
            request.skill_ref,
            request.expected_world_revision,
        )
        actual_run_identity = (
            run.session_id,
            run.turn_id,
            run.command_id,
            run.world_id,
            run.skill_ref,
            run.world_revision_before,
        )
        if actual_run_identity != expected_run_identity:
            raise AgentToolExecutionError(
                "TOOL_RUN_IDENTITY_MISMATCH",
                "SkillInvocationPort returned a run outside this accepted turn",
            )
        world_commit_evidence = tuple(
            item for item in run.evidence_refs if item.evidence_type is EvidenceType.WORLD_COMMIT
        )
        committed = run.world_revision_after == run.world_revision_before + 1
        if run.world_revision_after not in {
            run.world_revision_before,
            run.world_revision_before + 1,
        }:
            raise AgentToolExecutionError(
                "TOOL_WORLD_REVISION_INVALID",
                "one Skill invocation may advance the world by at most one revision",
            )
        if committed != (len(world_commit_evidence) == 1):
            raise AgentToolExecutionError(
                "TOOL_WORLD_COMMIT_EVIDENCE_MISMATCH",
                "world revision and WORLD_COMMIT Evidence do not describe the same commit",
            )
        if run.task_success and not committed:
            raise AgentToolExecutionError(
                "TOOL_FALSE_SUCCESS_RECEIPT",
                "a successful world task requires an exact +1 World commit receipt",
            )
        return ToolResult(
            value={
                "run_id": run.run_id,
                "task_success": run.task_success,
                "world_revision_before": run.world_revision_before,
                "world_revision_after": run.world_revision_after,
                "world_difference": run.world_difference,
                "failed_actions": run.failed_actions,
            },
            summary={
                "run_id": run.run_id,
                "task_success": run.task_success,
                "world_revision_before": run.world_revision_before,
                "world_revision_after": run.world_revision_after,
                "world_difference": run.world_difference,
                "evidence_ids": [item.evidence_id for item in run.evidence_refs],
            },
            evidence_refs=run.evidence_refs,
        )


def _empty_schema(_: TurnContext) -> Mapping[str, object]:
    return {"type": "object", "additionalProperties": False, "required": [], "properties": {}}


def _invoke_schema(context: TurnContext) -> Mapping[str, object]:
    if context.skill is None:
        raise AgentConfigurationError(
            "TOOL_CONTEXT_INCOMPLETE",
            "invoke_skill schema requires a bound skill",
        )
    rendered = thaw_value(context.skill.parameter_schema)
    if not isinstance(rendered, Mapping):
        raise AgentConfigurationError(
            "TOOL_SCHEMA_INVALID",
            "bound Skill parameter schema must be an object",
        )
    arguments_schema = dict(cast(Mapping[str, object], rendered))
    # Certification metadata is part of the immutable at-rest Skill closure,
    # not a JSON Schema keyword and never model-controlled tool input.
    arguments_schema.pop("x-yaya-certification", None)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["skill_id", "arguments"],
        "properties": {
            "skill_id": {"type": "string", "const": _BOUND_SKILL_ALIAS},
            "arguments": arguments_schema,
        },
    }


def build_default_tool_registry(
    trace: AgentTracePort,
    invocations: SkillInvocationPort,
) -> ToolRegistry:
    registry = ToolRegistry(trace)

    async def get_current_task(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        value = {
            "task_id": context.task.task_id,
            "title": context.task.title,
            "goal": context.task.goal,
            "knowledge_points": context.task.knowledge_points,
        }
        return ToolResult(value, value)

    async def get_world_summary(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        if context.world is None:
            raise AgentToolExecutionError("TOOL_CONTEXT_INCOMPLETE", "world summary is unavailable")
        value = {
            "world_id": context.world.world_id,
            "revision": context.world.revision,
            "last_event_sequence": context.world.last_event_sequence,
            "state_hash": context.world.state_hash,
            "visible_state": context.world.visible_state,
        }
        return ToolResult(value, value)

    async def list_student_skills(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        skills = [
            {
                "binding": (
                    _BOUND_SKILL_ALIAS
                    if context.skill is not None and item.ref == context.skill.ref
                    else f"available_skill_{index}"
                ),
                "parameter_schema": item.parameter_schema,
            }
            for index, item in enumerate(context.available_skills, start=1)
        ]
        return ToolResult({"skills": skills}, {"skills": skills})

    async def get_current_skill(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        if context.skill is None:
            raise AgentToolExecutionError("TOOL_CONTEXT_INCOMPLETE", "bound skill is unavailable")
        value = {
            "binding": _BOUND_SKILL_ALIAS,
            "source_code": context.skill.source_code,
            "entrypoint": context.skill.entrypoint,
            "parameter_schema": context.skill.parameter_schema,
        }
        summary = {
            "binding": _BOUND_SKILL_ALIAS,
            "source_chars": len(context.skill.source_code),
            "entrypoint": context.skill.entrypoint,
        }
        return ToolResult(value, summary)

    async def get_current_run(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        if context.run_result is None:
            raise AgentToolExecutionError("TOOL_CONTEXT_INCOMPLETE", "current run is unavailable")
        value = _run_projection(context.run_result)
        return ToolResult(value, value, context.run_result.evidence_refs)

    async def get_learner_profile(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        if context.learner_profile is None:
            raise AgentToolExecutionError(
                "TOOL_CONTEXT_INCOMPLETE", "learner profile is unavailable"
            )
        value = {
            "student_id": context.learner_profile.student_id,
            "revision": context.learner_profile.revision,
            "competencies": context.learner_profile.competencies,
        }
        return ToolResult(value, value, context.learner_profile.evidence_refs)

    async def get_task_tests_summary(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        cases = [
            {
                "case_id": item.case_id,
                "failure_key": item.failure_key,
                "title": item.title,
                "input": item.input,
                "observed": item.observed,
                "evidence_ids": [ref.evidence_id for ref in item.evidence_refs],
            }
            for item in context.counterexamples
        ]
        evidence = _merge_evidence(
            tuple(ref for item in context.counterexamples for ref in item.evidence_refs)
        )
        return ToolResult({"counterexamples": cases}, {"counterexamples": cases}, evidence)

    async def get_skill_history(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        history = [
            {
                "skill_id": item.skill_id,
                "skill_version_id": item.skill_version_id,
                "source_sha256": item.source_sha256,
                "change_summary": item.change_summary,
            }
            for item in context.skill_history
        ]
        return ToolResult({"versions": history}, {"versions": history})

    async def get_session_runs(
        _: FrozenObject,
        context: TurnContext,
        __: ToolExecutionContext,
        ___: OperationContext,
    ) -> ToolResult:
        runs = [_run_projection(item) for item in context.session_runs]
        evidence = _merge_evidence(
            tuple(ref for item in context.session_runs for ref in item.evidence_refs)
        )
        return ToolResult({"runs": runs}, {"runs": runs}, evidence)

    definitions: tuple[AgentTool, ...] = (
        AgentTool(
            "get_current_task",
            "Return the validated current task summary.",
            _empty_schema,
            frozenset({"world_agent", "teaching_agent"}),
            FunctionToolHandler(get_current_task),
        ),
        AgentTool(
            "get_world_summary",
            "Return the canonical bounded world summary for this turn.",
            _empty_schema,
            frozenset({"world_agent", "xiaohutao"}),
            FunctionToolHandler(get_world_summary),
            is_available=lambda context: context.world is not None,
        ),
        AgentTool(
            "list_student_skills",
            "List certified active skills already loaded for this learner.",
            _empty_schema,
            frozenset({"xiaohutao"}),
            FunctionToolHandler(list_student_skills),
        ),
        AgentTool(
            "get_current_skill",
            "Return the exact certified skill bound to this turn.",
            _empty_schema,
            frozenset({"xiaohutao", "teaching_agent", "bug_agent"}),
            FunctionToolHandler(get_current_skill),
            is_available=lambda context: context.skill is not None,
        ),
        AgentTool(
            "invoke_skill",
            "Run the exact certified skill through the Sandbox/World application use case.",
            _invoke_schema,
            frozenset({"xiaohutao"}),
            InvokeSkillToolHandler(invocations),
            is_available=lambda context: context.skill is not None and context.world is not None,
        ),
        AgentTool(
            "get_current_run",
            "Return the exact run bound to this turn, never an unscoped latest run.",
            _empty_schema,
            frozenset({"teaching_agent", "bug_agent"}),
            FunctionToolHandler(get_current_run),
            is_available=lambda context: context.run_result is not None,
        ),
        AgentTool(
            "get_learner_profile",
            "Return the task-scoped learner projection and its revision.",
            _empty_schema,
            frozenset({"teaching_agent", "book_agent"}),
            FunctionToolHandler(get_learner_profile),
            is_available=lambda context: context.learner_profile is not None,
        ),
        AgentTool(
            "get_task_tests_summary",
            "Return only evidence-backed counterexamples for the current failure class.",
            _empty_schema,
            frozenset({"bug_agent"}),
            FunctionToolHandler(get_task_tests_summary),
        ),
        AgentTool(
            "get_skill_history",
            "Return skill versions recorded in this session.",
            _empty_schema,
            frozenset({"book_agent"}),
            FunctionToolHandler(get_skill_history),
        ),
        AgentTool(
            "get_session_runs",
            "Return canonical runs from this session through the completion run.",
            _empty_schema,
            frozenset({"book_agent"}),
            FunctionToolHandler(get_session_runs),
        ),
    )
    for definition in definitions:
        registry.register(definition)
    return registry


def _run_projection(run: RunResultSnapshot) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "task_success": run.task_success,
        "world_revision_before": run.world_revision_before,
        "world_revision_after": run.world_revision_after,
        "world_difference": run.world_difference,
        "failed_actions": run.failed_actions,
        "failure_key": run.failure_key,
        "evidence_ids": [item.evidence_id for item in run.evidence_refs],
    }


def _merge_evidence(items: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    merged: dict[str, EvidenceRef] = {}
    for raw_item in items:
        previous = merged.get(raw_item.evidence_id)
        if previous is not None and previous != raw_item:
            raise AgentToolExecutionError(
                "TOOL_EVIDENCE_COLLISION",
                "same evidence_id carries different immutable metadata",
                {"evidence_id": raw_item.evidence_id},
            )
        merged[raw_item.evidence_id] = raw_item
    return tuple(merged.values())


__all__ = [
    "AgentTool",
    "FunctionToolHandler",
    "InvokeSkillToolHandler",
    "ToolExecutionContext",
    "ToolHandler",
    "ToolRegistry",
    "build_default_tool_registry",
    "side_effect_execution_id",
]
