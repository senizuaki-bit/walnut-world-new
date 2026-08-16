"""Deterministic Evidence catalog and provider-safe aliases for one turn."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from yaya_agent_contracts import EvidenceRef

from .domain import TurnContext
from .errors import AgentContextError


def collect_context_evidence(context: TurnContext) -> tuple[EvidenceRef, ...]:
    refs = list(context.event.evidence_refs)
    if context.compile_result is not None:
        refs.extend(context.compile_result.evidence_refs)
    if context.run_result is not None:
        refs.extend(context.run_result.evidence_refs)
    for run in (*context.failure_history, *context.session_runs):
        refs.extend(run.evidence_refs)
    for case in context.counterexamples:
        refs.extend(case.evidence_refs)
    if context.learner_profile is not None:
        refs.extend(context.learner_profile.evidence_refs)

    merged: dict[str, EvidenceRef] = {}
    for ref in refs:
        previous = merged.get(ref.evidence_id)
        if previous is not None and previous != ref:
            raise AgentContextError(
                "EVIDENCE_IDENTITY_COLLISION",
                "same evidence_id carries different immutable context metadata",
                {"evidence_id": ref.evidence_id},
            )
        merged[ref.evidence_id] = ref
    return tuple(merged.values())


def collect_decision_evidence(context: TurnContext) -> tuple[EvidenceRef, ...]:
    """Evidence owned by this feedback, excluding historical prompt-only facts."""

    refs = list(context.event.evidence_refs)
    if context.compile_result is not None:
        refs.extend(context.compile_result.evidence_refs)
    if context.run_result is not None:
        refs.extend(context.run_result.evidence_refs)
    merged: dict[str, EvidenceRef] = {}
    for ref in refs:
        previous = merged.get(ref.evidence_id)
        if previous is not None and previous != ref:
            raise AgentContextError(
                "EVIDENCE_IDENTITY_COLLISION",
                "same evidence_id carries different decision metadata",
                {"evidence_id": ref.evidence_id},
            )
        merged[ref.evidence_id] = ref
    if len(merged) > 64:
        raise AgentContextError(
            "EVIDENCE_LIMIT_EXCEEDED",
            "Agent feedback cannot retain its complete owning Evidence set",
            {"maximum": 64, "actual": len(merged)},
        )
    return tuple(merged.values())


def build_evidence_aliases(
    context: TurnContext,
) -> tuple[dict[str, str], dict[str, str]]:
    aliases: dict[str, str] = {}
    types: dict[str, str] = {}
    for ref in collect_context_evidence(context):
        aliases[ref.evidence_id] = f"evidence_{len(aliases) + 1:03d}"
        types[ref.evidence_id] = ref.evidence_type.value
    return aliases, types


def alias_evidence_refs(
    refs: Sequence[EvidenceRef],
    aliases: Mapping[str, str],
) -> list[str]:
    values: list[str] = []
    for ref in refs:
        try:
            values.append(aliases[ref.evidence_id])
        except KeyError as error:
            raise AgentContextError(
                "EVIDENCE_ALIAS_MISSING",
                "Evidence alias catalog is incomplete",
                {"evidence_id": ref.evidence_id},
            ) from error
    return values


__all__ = [
    "alias_evidence_refs",
    "build_evidence_aliases",
    "collect_context_evidence",
    "collect_decision_evidence",
]
