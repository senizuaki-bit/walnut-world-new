"""Shared bounded Agent runtime for all five game roles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import cast

from yaya_agent_contracts import (
    EvidenceRef,
    Failure,
    FrozenJsonObject,
    LlmPort,
    LlmReply,
    LlmRequest,
    OperationContext,
    Success,
    VersionSet,
)

from .domain import (
    AgentDecision,
    AgentTraceEvent,
    RoleId,
    SkillInvocationResult,
    SkillRecoveryContext,
    ToolCallRecord,
    TurnContext,
)
from .errors import (
    AgentDependencyError,
    AgentToolError,
    AgentToolExecutionError,
    InvalidAgentOutput,
    RuntimeBoundaryError,
    RuntimeBoundaryStage,
)
from .evidence import build_evidence_aliases, collect_decision_evidence
from .fallbacks import fallback_for
from .model_output import (
    ModelDecisionEnvelope,
    ModelEnvelope,
    ModelToolCallsEnvelope,
    build_model_output_schema,
    parse_model_envelope,
)
from .ports import AgentTracePort
from .prompting import PromptBuilder
from .recovery import recover_skill_invocation_decision
from .role_config import RoleConfigProvider
from .tool_registry import ToolRegistry
from .validators import validate_decision


class SharedAgentRuntime:
    """One role, at most one tool round and one invalid-output repair."""

    def __init__(
        self,
        *,
        llm: LlmPort,
        role_configs: RoleConfigProvider,
        tools: ToolRegistry,
        prompts: PromptBuilder,
        trace: AgentTracePort,
        versions: VersionSet,
        clock: Callable[[], datetime],
    ) -> None:
        self._llm = llm
        self._role_configs = role_configs
        self._tools = tools
        self._prompts = prompts
        self._trace = trace
        self._versions = versions
        self._clock = clock

    def execution_budget_ms(self, role: RoleId) -> int:
        """Upper bound used to fence a side-effecting turn lease."""

        config = self._role_configs.get(role)
        # The bounded state machine can make three serial model requests
        # (initial, post-tool, and one repair) and execute every accepted tool
        # call serially. Trace writes are themselves bounded to one second; the
        # fixed headroom covers all trace points plus commit scheduling jitter.
        max_model_requests = 3
        trace_and_commit_headroom_ms = 15_000
        return (
            config.timeout_ms * (max_model_requests + config.limits.max_tool_calls)
            + trace_and_commit_headroom_ms
        )

    async def recover_skill_invocation(
        self,
        scope: SkillRecoveryContext,
        result: SkillInvocationResult,
        operation_context: OperationContext,
        *,
        runtime_warnings: Sequence[str] = (),
    ) -> AgentDecision:
        """Render a durable receipt after response loss without another model/tool call."""

        warnings = list(dict.fromkeys(runtime_warnings))
        _add_warning(
            warnings,
            await _record_trace(
                self._trace,
                AgentTraceEvent(
                    "agent.turn.recovered",
                    scope.event.turn_id,
                    "xiaohutao",
                    {
                        "event_id": scope.event.event_id,
                        "run_id": result.run.run_id,
                        "task_success": result.run.task_success,
                    },
                ),
                operation_context,
            ),
        )
        return recover_skill_invocation_decision(
            scope,
            result,
            operation_context,
            completed_at=_decision_completed_at(
                self._clock,
                scope.event.occurred_at,
                scope.event.evidence_refs,
                result.run.evidence_refs,
            ),
            runtime_warnings=tuple(warnings),
        )

    async def run(
        self,
        role: RoleId,
        context: TurnContext,
        operation_context: OperationContext,
    ) -> AgentDecision:
        if context.role != role:
            raise AgentDependencyError(
                "RUNTIME_CONTEXT_ROLE_MISMATCH",
                "runtime received a context for a different role",
                {"expected": role, "actual": context.role},
            )
        config = self._role_configs.get(role)
        if context.event.event_type not in config.allowed_events:
            raise AgentDependencyError(
                "RUNTIME_EVENT_NOT_ALLOWED",
                "runtime role configuration rejects the event",
                {"role": role, "event_type": context.event.event_type},
            )

        directive = context.teaching_directive
        patch_eligible = directive is not None and directive.patch_eligible
        tool_definitions = (
            ()
            if patch_eligible
            else self._tools.model_definitions(role, config.allowed_tools, context)
        )
        max_tool_calls = 0 if patch_eligible else config.limits.max_tool_calls
        evidence_aliases, _ = build_evidence_aliases(context)
        current_evidence_ids = {item.evidence_id for item in collect_decision_evidence(context)}
        required_aliases = (
            ()
            if directive is None
            or not set(directive.required_evidence_ids).issubset(current_evidence_ids)
            else tuple(evidence_aliases[item] for item in directive.required_evidence_ids)
        )
        output_schema = build_model_output_schema(
            tool_definitions,
            max_tool_calls=max_tool_calls,
            role=role,
            directive=directive,
            required_evidence_aliases=required_aliases,
        )
        messages = self._prompts.initial_messages(config, context, tool_definitions)
        runtime_warnings: list[str] = []
        _add_warning(
            runtime_warnings,
            await _record_trace(
                self._trace,
                AgentTraceEvent(
                    "agent.turn.started",
                    context.event.turn_id,
                    role,
                    {
                        "event_id": context.event.event_id,
                        "event_type": context.event.event_type,
                        "session_id": context.event.session_id,
                        "tool_count": len(tool_definitions),
                        "hint_level": context.hint_level,
                    },
                ),
                operation_context,
            ),
        )

        invalid_attempts = 0
        tool_round_complete = False
        side_effect_dispatched = False
        records: list[ToolCallRecord] = []
        tool_evidence: list[EvidenceRef] = []
        total_input_tokens = 0
        total_output_tokens = 0
        request_count = 0

        while True:
            request_count += 1
            request = LlmRequest(
                messages=messages,
                output_schema=cast(FrozenJsonObject, output_schema),
                temperature=config.temperature,
                max_output_tokens=config.max_output_tokens,
                timeout_ms=config.timeout_ms,
                versions=self._versions,
            )
            _add_warning(
                runtime_warnings,
                await _record_trace(
                    self._trace,
                    AgentTraceEvent(
                        "agent.model.requested",
                        context.event.turn_id,
                        role,
                        {
                            "request_number": request_count,
                            "message_count": len(messages),
                            "tool_round_complete": tool_round_complete,
                            "session_run_count": len(context.session_runs),
                            "skill_history_versions": [
                                item.skill_version_id for item in context.skill_history
                            ],
                        },
                    ),
                    operation_context,
                ),
            )
            try:
                result = await _at_async_runtime_boundary(
                    RuntimeBoundaryStage.LLM_GENERATE,
                    lambda: asyncio.wait_for(
                        self._llm.generate(request, operation_context),
                        timeout=request.timeout_ms / 1000,
                    ),
                )
            except TimeoutError:
                return await self._fallback_or_raise(
                    role,
                    context,
                    reason="LLM_TIMEOUT",
                    records=tuple(records),
                    evidence=tool_evidence,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    runtime_warnings=runtime_warnings,
                    operation_context=operation_context,
                )
            except ConnectionError:
                return await self._fallback_or_raise(
                    role,
                    context,
                    reason="DEPENDENCY_UNAVAILABLE",
                    records=tuple(records),
                    evidence=tool_evidence,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    runtime_warnings=runtime_warnings,
                    operation_context=operation_context,
                )
            if isinstance(result, Failure):
                if _is_repairable_model_failure(result) and invalid_attempts == 0:
                    invalid_attempts += 1
                    await self._record_invalid(
                        context,
                        role,
                        result.error.code,
                        result.error.details,
                        invalid_attempts,
                        runtime_warnings,
                        operation_context,
                    )
                    messages = self._prompts.after_validation_error(
                        messages,
                        role=role,
                        error_code=result.error.code,
                        details=result.error.details,
                        final_only=tool_round_complete,
                        directive=context.teaching_directive,
                        required_evidence_aliases=required_aliases,
                    )
                    continue
                return await self._fallback_or_raise(
                    role,
                    context,
                    reason=_model_failure_reason(result),
                    records=tuple(records),
                    evidence=tool_evidence,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    runtime_warnings=runtime_warnings,
                    operation_context=operation_context,
                )
            if not isinstance(result, Success) or not isinstance(result.value, LlmReply):
                raise AgentDependencyError(
                    "LLM_PORT_CONTRACT_VIOLATION",
                    "LlmPort returned a value outside Result[LlmReply]",
                    {"actual_type": type(result).__name__},
                )
            reply = result.value
            total_input_tokens += reply.input_tokens
            total_output_tokens += reply.output_tokens
            if (
                role in {"bug_agent", "book_agent"}
                or context.event.event_type == "skill_patch_requested"
            ) and (reply.source != "provider" or reply.degraded):
                return await self._fallback_or_raise(
                    role,
                    context,
                    reason=reply.fallback_reason or "DEGRADED_PROVIDER_REPLY",
                    records=tuple(records),
                    evidence=tool_evidence,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    runtime_warnings=runtime_warnings,
                    operation_context=operation_context,
                )
            try:
                if reply.evidence_refs:
                    raise InvalidAgentOutput(
                        "MODEL_EVIDENCE_FORBIDDEN",
                        "a model reply cannot mint or attach authoritative Evidence",
                    )
                raw_decision = (
                    reply.output.get("decision") if isinstance(reply.output, Mapping) else None
                )
                if (
                    not patch_eligible
                    and isinstance(raw_decision, Mapping)
                    and raw_decision.get("response_type") == "skill_patch"
                ):
                    raise InvalidAgentOutput(
                        "RESPONSE_TYPE_DIRECTIVE_MISMATCH",
                        "the response type is not permitted by the authoritative directive",
                    )
                envelope: ModelEnvelope = _at_runtime_boundary(
                    RuntimeBoundaryStage.PARSE_MODEL_ENVELOPE,
                    lambda: parse_model_envelope(
                        reply.output,
                        patch_authority=(context.patch_authority if patch_eligible else None),
                    ),
                )
                if isinstance(envelope, ModelToolCallsEnvelope):
                    if reply.degraded:
                        raise InvalidAgentOutput(
                            "DEGRADED_TOOL_CALL_FORBIDDEN",
                            "a provider fallback cannot initiate side effects",
                        )
                    if tool_round_complete:
                        raise InvalidAgentOutput(
                            "TOOL_LOOP_LIMIT",
                            "the single allowed tool round has already completed",
                        )
                    if len(envelope.calls) > max_tool_calls:
                        raise InvalidAgentOutput(
                            "TOOL_CALL_LIMIT_EXCEEDED",
                            "model requested more tools than the role limit",
                            {
                                "maximum": max_tool_calls,
                                "actual": len(envelope.calls),
                            },
                        )
                    invoke_count = sum(call.name == "invoke_skill" for call in envelope.calls)
                    if invoke_count > 1:
                        raise InvalidAgentOutput(
                            "SIDE_EFFECT_TOOL_DUPLICATED",
                            "invoke_skill may appear at most once in one turn",
                        )
                    if invoke_count == 1 and envelope.calls[-1].name != "invoke_skill":
                        raise InvalidAgentOutput(
                            "SIDE_EFFECT_TOOL_MUST_BE_LAST",
                            "read-only tools must finish before the side-effect tool",
                        )
                    try:
                        for call in envelope.calls:
                            self._tools.validate_call(
                                role=role,
                                allowed_names=config.allowed_tools,
                                name=call.name,
                                arguments=call.arguments,
                                turn_context=context,
                            )
                    except AgentToolError as error:
                        raise InvalidAgentOutput(
                            "MODEL_TOOL_CALL_REJECTED",
                            "the complete tool batch failed preflight before execution",
                            {"tool_error_code": error.code, **dict(error.details)},
                        ) from error
                    summaries: list[Mapping[str, object]] = []
                    for ordinal, call in enumerate(envelope.calls, start=1):
                        if call.name == "invoke_skill" and tool_evidence:
                            raise AgentToolExecutionError(
                                "SIDE_EFFECT_EVIDENCE_BUDGET_UNSAFE",
                                "side-effect execution requires an empty Evidence budget",
                                {"existing_evidence": len(tool_evidence)},
                            )
                        try:
                            if call.name == "invoke_skill":
                                side_effect_dispatched = True
                            record, tool_result, tool_warnings = await asyncio.wait_for(
                                self._tools.execute(
                                    role=role,
                                    allowed_names=config.allowed_tools,
                                    model_call_id=call.call_id,
                                    ordinal=ordinal,
                                    name=call.name,
                                    arguments=call.arguments,
                                    turn_context=context,
                                    operation_context=operation_context,
                                ),
                                timeout=config.timeout_ms / 1000,
                            )
                        except TimeoutError as error:
                            raise AgentToolExecutionError(
                                "TOOL_TIMEOUT",
                                "tool execution exceeded the role timeout",
                                {"tool": call.name, "timeout_ms": config.timeout_ms},
                            ) from error
                        records.append(record)
                        for warning in tool_warnings:
                            _add_warning(runtime_warnings, warning)
                        summaries.append(tool_result.summary)
                        _extend_evidence(
                            tool_evidence,
                            tool_result.evidence_refs,
                            base=collect_decision_evidence(context),
                        )
                    messages = self._prompts.after_tools(
                        messages,
                        envelope.calls,
                        tuple(summaries),
                        role=role,
                        directive=context.teaching_directive,
                        required_evidence_aliases=required_aliases,
                    )
                    tool_round_complete = True
                    continue
                if not isinstance(envelope, ModelDecisionEnvelope):
                    raise AssertionError("unreachable model envelope")
                draft = envelope.draft
                validated_draft = _at_runtime_boundary(
                    RuntimeBoundaryStage.VALIDATE_DECISION,
                    lambda: validate_decision(
                        draft,
                        config,
                        context,
                        tuple(records),
                    ),
                )
            except AgentToolError as error:
                if side_effect_dispatched and error.details.get("commit_state") != "ROLLED_BACK":
                    _add_warning(runtime_warnings, "SIDE_EFFECT_COMMIT_UNKNOWN")
                warning = error.details.get("runtime_warning")
                if isinstance(warning, str):
                    _add_warning(runtime_warnings, warning)
                warnings = error.details.get("runtime_warnings")
                if isinstance(warnings, Sequence) and not isinstance(
                    warnings, (str, bytes, bytearray)
                ):
                    for item in cast(Sequence[object], warnings):
                        if isinstance(item, str):
                            _add_warning(runtime_warnings, item)
                return await self._fallback_or_raise(
                    role,
                    context,
                    reason=error.code,
                    records=tuple(records),
                    evidence=tool_evidence,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    runtime_warnings=runtime_warnings,
                    operation_context=operation_context,
                )
            except InvalidAgentOutput as error:
                if invalid_attempts == 0:
                    invalid_attempts += 1
                    await self._record_invalid(
                        context,
                        role,
                        error.code,
                        error.details,
                        invalid_attempts,
                        runtime_warnings,
                        operation_context,
                    )
                    messages = self._prompts.after_validation_error(
                        messages,
                        role=role,
                        error_code=error.code,
                        details=error.details,
                        final_only=tool_round_complete,
                        directive=context.teaching_directive,
                        required_evidence_aliases=required_aliases,
                    )
                    continue
                return await self._fallback_or_raise(
                    role,
                    context,
                    reason="MODEL_OUTPUT_INVALID",
                    records=tuple(records),
                    evidence=tool_evidence,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    runtime_warnings=runtime_warnings,
                    operation_context=operation_context,
                )

            evidence = _at_runtime_boundary(
                RuntimeBoundaryStage.MERGE_EVIDENCE,
                lambda: _merge_evidence(
                    collect_decision_evidence(context),
                    tool_evidence,
                    reply.evidence_refs,
                ),
            )
            completed_at = _at_runtime_boundary(
                RuntimeBoundaryStage.DECISION_TIME,
                lambda: _decision_completed_at(
                    self._clock,
                    context.event.occurred_at,
                    evidence,
                ),
            )
            decision = _at_runtime_boundary(
                RuntimeBoundaryStage.CONSTRUCT_AGENT_DECISION,
                lambda: AgentDecision(
                    draft=validated_draft,
                    message_key=f"agent.{role}.{validated_draft.response_type}",
                    source=reply.source,
                    degraded=reply.degraded,
                    fallback_reason=reply.fallback_reason,
                    provider=reply.provider,
                    model=reply.model,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    tool_calls=tuple(records),
                    evidence_refs=evidence,
                    completed_at=completed_at,
                    runtime_warnings=tuple(runtime_warnings),
                    teaching_directive=context.teaching_directive,
                ),
            )
            warning = await _record_trace(
                self._trace,
                AgentTraceEvent(
                    "agent.turn.finished",
                    context.event.turn_id,
                    role,
                    {
                        "validated": True,
                        "fallback": decision.degraded,
                        "fallback_reason": decision.fallback_reason,
                        "model_provider": decision.provider,
                        "model": decision.model,
                        "model_requests": request_count,
                        "tool_calls": len(records),
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                ),
                operation_context,
            )
            if warning is not None:
                _add_warning(runtime_warnings, warning)
                decision = replace(decision, runtime_warnings=tuple(runtime_warnings))
            return decision

    async def _fallback_or_raise(
        self,
        role: RoleId,
        context: TurnContext,
        *,
        reason: str,
        records: tuple[ToolCallRecord, ...],
        evidence: Sequence[EvidenceRef],
        input_tokens: int,
        output_tokens: int,
        runtime_warnings: list[str],
        operation_context: OperationContext,
    ) -> AgentDecision:
        """Keep Bug/Book trust-boundary failures out of durable projections.

        Generic conversational roles retain the product's explicit degraded
        response.  Bug and Book decisions, however, are the evidence-backed
        acceptance records for this vertical slice.  Persisting a fallback
        after invalid role, directive, Evidence, or mastery claims would turn
        an untrusted provider response into an apparently canonical teaching
        result.  These two roles therefore terminate without an AgentDecision;
        the fenced Worker records the Command failure instead.
        """

        if role in {"bug_agent", "book_agent"} or (
            context.event.event_type == "skill_patch_requested"
        ):
            _add_warning(
                runtime_warnings,
                await _record_trace(
                    self._trace,
                    AgentTraceEvent(
                        "agent.turn.failed",
                        context.event.turn_id,
                        role,
                        {
                            "reason": reason,
                            "model_requests_failed_closed": True,
                            "tool_calls": len(records),
                        },
                    ),
                    operation_context,
                ),
            )
            raise AgentDependencyError(
                "DIRECTIVE_PROVIDER_OUTPUT_UNTRUSTED",
                "evidence-authority output could not be trusted for durable publication",
                {"reason": reason},
            )
        return await self._fallback(
            role,
            context,
            reason=reason,
            records=records,
            evidence=evidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            runtime_warnings=runtime_warnings,
            operation_context=operation_context,
        )

    async def _record_invalid(
        self,
        context: TurnContext,
        role: RoleId,
        error_code: str,
        details: Mapping[str, object],
        attempt: int,
        runtime_warnings: list[str],
        operation_context: OperationContext,
    ) -> None:
        trace_fields: dict[str, object] = {
            "error_code": error_code,
            "repair_attempt": attempt,
        }
        validation_error = details.get("validation_error")
        if isinstance(validation_error, str):
            trace_fields["validation_error"] = validation_error[:300]
        output_shape = details.get("output_shape")
        if isinstance(output_shape, (Mapping, list, tuple)):
            trace_fields["output_shape"] = output_shape
        _add_warning(
            runtime_warnings,
            await _record_trace(
                self._trace,
                AgentTraceEvent(
                    "agent.output.invalid",
                    context.event.turn_id,
                    role,
                    trace_fields,
                ),
                operation_context,
            ),
        )

    async def _fallback(
        self,
        role: RoleId,
        context: TurnContext,
        *,
        reason: str,
        records: tuple[ToolCallRecord, ...],
        evidence: Sequence[EvidenceRef],
        input_tokens: int,
        output_tokens: int,
        runtime_warnings: list[str],
        operation_context: OperationContext,
    ) -> AgentDecision:
        _add_warning(
            runtime_warnings,
            await _record_trace(
                self._trace,
                AgentTraceEvent(
                    "agent.turn.finished",
                    context.event.turn_id,
                    role,
                    {
                        "validated": True,
                        "fallback": True,
                        "fallback_reason": reason,
                        "tool_calls": len(records),
                    },
                ),
                operation_context,
            ),
        )
        decision = fallback_for(
            role,
            context,
            reason=reason,
            tool_calls=records,
            tool_evidence=evidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            runtime_warnings=tuple(runtime_warnings),
            completed_at=_decision_completed_at(
                self._clock,
                context.event.occurred_at,
                collect_decision_evidence(context),
                evidence,
            ),
        )
        return decision


async def _at_async_runtime_boundary[BoundaryValue](
    stage: RuntimeBoundaryStage,
    operation: Callable[[], Awaitable[BoundaryValue]],
) -> BoundaryValue:
    """Async counterpart of ``_at_runtime_boundary``."""

    try:
        return await operation()
    except ValueError:
        pass
    raise RuntimeBoundaryError(stage) from None


def _at_runtime_boundary[BoundaryValue](
    stage: RuntimeBoundaryStage,
    operation: Callable[[], BoundaryValue],
) -> BoundaryValue:
    """Replace a raw ValueError with a stage-only, context-free diagnostic."""

    try:
        return operation()
    except ValueError:
        pass
    # Raise after leaving the handler so the sensitive source exception is not
    # retained as __context__ on the redacted boundary error.
    raise RuntimeBoundaryError(stage) from None


def _decision_completed_at(
    clock: Callable[[], datetime],
    event_occurred_at: datetime,
    *evidence_groups: Sequence[EvidenceRef],
) -> datetime:
    """Keep a decision causally after its database-authored inputs."""

    candidates = [clock(), event_occurred_at]
    candidates.extend(item.created_at for group in evidence_groups for item in group)
    return max(candidates)


def _is_repairable_model_failure(result: Failure) -> bool:
    return (
        result.error.code == "INVARIANT_VIOLATION"
        and result.error.stage == "MODEL_OUTPUT"
        and result.error.details.get("repairable", True) is not False
    )


def _model_failure_reason(result: Failure) -> str:
    if result.error.code == "INVARIANT_VIOLATION" and result.error.stage == "MODEL_OUTPUT":
        return "MODEL_OUTPUT_INVALID"
    return result.error.code


def _extend_evidence(
    target: list[EvidenceRef],
    items: Sequence[EvidenceRef],
    *,
    base: Sequence[EvidenceRef] = (),
) -> None:
    by_id = {item.evidence_id: item for item in base}
    for item in target:
        previous = by_id.get(item.evidence_id)
        if previous is not None and previous != item:
            raise AgentToolExecutionError(
                "EVIDENCE_IDENTITY_COLLISION",
                "same evidence_id carries different immutable metadata",
                {"evidence_id": item.evidence_id},
            )
        by_id[item.evidence_id] = item
    for item in items:
        previous = by_id.get(item.evidence_id)
        if previous is not None and previous != item:
            raise AgentToolExecutionError(
                "EVIDENCE_IDENTITY_COLLISION",
                "same evidence_id carries different immutable metadata",
                {"evidence_id": item.evidence_id},
            )
        if previous is None:
            if len(by_id) >= 64:
                raise AgentToolExecutionError(
                    "EVIDENCE_LIMIT_EXCEEDED",
                    "Agent feedback cannot retain more than 64 immutable Evidence references",
                    {"maximum": 64},
                )
            target.append(item)
            by_id[item.evidence_id] = item


def _merge_evidence(*groups: Sequence[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    merged: list[EvidenceRef] = []
    for group in groups:
        _extend_evidence(merged, group)
    return tuple(merged)


async def _record_trace(
    trace: AgentTracePort,
    event: AgentTraceEvent,
    operation_context: OperationContext,
) -> str | None:
    try:
        await asyncio.wait_for(trace.record(event, operation_context), timeout=1.0)
    except Exception:
        return "TRACE_WRITE_FAILED"
    return None


def _add_warning(target: list[str], warning: str | None) -> None:
    if warning is not None and warning not in target:
        target.append(warning)


__all__ = ["SharedAgentRuntime"]
