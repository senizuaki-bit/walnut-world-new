"""Purpose-bound Feishu projections over authoritative learner data.

This module deliberately owns no learner mutation port.  Its only write-shaped
port appends a redacted access audit after each allowed, denied, or failed query.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar, cast

from yaya_agent_contracts import (
    ContentRef,
    ContractError,
    ErrorCategory,
    Failure,
    OperationContext,
    Result,
    Success,
    canonical_json_sha256,
)

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
ALLOWED_ACTOR_TYPES = frozenset({"teacher", "operator", "service"})
LEARNER_PURPOSES = frozenset(
    {"TEACHER_SUPPORT", "GUARDIAN_REPORT", "LEARNING_REVIEW", "SAFETY_INVESTIGATION"}
)
LEARNER_CONSENT_BASES = frozenset({"EDUCATIONAL_SERVICE", "GUARDIAN_CONSENT", "LEGAL_SAFETY_DUTY"})
CLASS_PURPOSES = frozenset({"TEACHER_PLANNING", "CURRICULUM_REVIEW", "PROGRAM_EVALUATION"})
EVIDENCE_PURPOSES = frozenset(
    {"TEACHER_SUPPORT", "GUARDIAN_REPORT", "LEARNING_REVIEW", "SAFETY_INVESTIGATION"}
)
REDACTED_FIELDS = (
    "credentials",
    "direct_identifiers",
    "raw_chat_text",
    "raw_source_code",
)
EVIDENCE_FACT_NAMES = frozenset(
    {
        "ai_assistance_level",
        "attempt_count",
        "evidence_kind",
        "knowledge_stage",
        "main_error",
        "run_ref",
        "skill_patch_used",
        "task_ref",
        "task_result",
    }
)
_EVIDENCE_TYPES = frozenset(
    {
        "ACTION_LOG",
        "AUDIT_LOG",
        "DOMAIN_EVENT",
        "LEARNER_UPDATE",
        "POLICY_DECISION",
        "SANDBOX_LOG",
        "TEST_REPORT",
        "WORLD_COMMIT",
    }
)
_MAX_QUERY_ROWS = 10_000
_TRANSPORT_AUDIT_RESOURCES = {
    "FEISHU_LEARNER_QUERY": "LEARNER_PROFILE",
    "FEISHU_CLASS_INSIGHTS": "CLASS_INSIGHTS",
}
_ResultValue = TypeVar("_ResultValue")


@dataclass(frozen=True, slots=True)
class LearnerProfileAuthority:
    learner_ref: str
    tenant_id: str
    learner_id: str
    actor_id: str
    content_hash: str
    profile: Mapping[str, Any]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LearningProjectionAuthority:
    job_id: str
    command_id: str
    session_id: str
    turn_id: str
    run_id: str
    learner_id: str
    source_event_id: str
    through_sequence: int
    projection: Mapping[str, Any]
    result: Mapping[str, Any]
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceAuthority:
    evidence_id: str
    command_id: str | None
    document: Mapping[str, Any]
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class LearnerLearningBundle:
    profile: LearnerProfileAuthority
    projections: tuple[LearningProjectionAuthority, ...]


@dataclass(frozen=True, slots=True)
class EvidenceLearningBundle:
    evidence: EvidenceAuthority
    profile: LearnerProfileAuthority
    projection: LearningProjectionAuthority
    learner_projections: tuple[LearningProjectionAuthority, ...]


class FeishuLearningStore(Protocol):
    async def learner_content_refs(
        self, tenant_id: str, learner_ref: str
    ) -> Result[tuple[ContentRef, ...]]: ...

    async def tenant_content_refs(self, tenant_id: str) -> Result[tuple[ContentRef, ...]]: ...

    async def learner_bundle(
        self,
        tenant_id: str,
        learner_ref: str,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
    ) -> Result[LearnerLearningBundle]: ...

    async def class_bundles(
        self,
        tenant_id: str,
        content_hash: str,
        occurred_from: datetime,
        occurred_to: datetime,
    ) -> Result[tuple[LearnerLearningBundle, ...]]: ...

    async def evidence_bundle(
        self, tenant_id: str, evidence_id: str
    ) -> Result[EvidenceLearningBundle]: ...

    async def append_access_audit(
        self,
        *,
        context: OperationContext,
        operation: str,
        outcome: Literal["ALLOWED", "DENIED", "FAILED"],
        resource_type: str,
        resource_id: str,
        purpose: str | None,
        evidence_ids: tuple[str, ...],
        error_code: str | None,
        details: Mapping[str, Any],
    ) -> Result[None]: ...


class FeishuLearningQueries:
    """Serialize teacher-readable facts without exposing mutable authority objects."""

    def __init__(
        self,
        store: FeishuLearningStore,
        *,
        pseudonym_secret: str,
        clock: Callable[[], datetime] | None = None,
        server_minimum_cohort_size: int = 5,
    ) -> None:
        if not 32 <= len(pseudonym_secret) <= 4096:
            raise ValueError("Feishu pseudonym secret must contain 32..4096 characters")
        if not 5 <= server_minimum_cohort_size <= 1000:
            raise ValueError("Feishu minimum cohort size must be between 5 and 1000")
        self._store = store
        self._secret = pseudonym_secret
        self._clock = clock or (lambda: datetime.now(UTC))
        self._minimum_cohort_size = server_minimum_cohort_size

    async def audit_transport_validation_failure(
        self,
        *,
        context: OperationContext,
        operation: str,
        validation_stage: Literal["JSON", "SCHEMA"],
    ) -> Result[None]:
        """Audit a rejected request without trusting any identity from its body."""
        resource_type = _TRANSPORT_AUDIT_RESOURCES.get(operation)
        if resource_type is None:
            raise ValueError("unsupported Feishu transport audit operation")
        return await self._store.append_access_audit(
            context=context,
            operation=operation,
            outcome="FAILED",
            resource_type=resource_type,
            resource_id="invalid",
            purpose=None,
            evidence_ids=(),
            error_code="INVALID_REQUEST",
            details={
                "validation_stage": validation_stage,
                "request_body_identity_used": False,
            },
        )

    async def resolve_learner_content_ref(
        self,
        learner_ref: str,
        context: OperationContext,
    ) -> Result[ContentRef]:
        """Resolve one learner's content only from tenant-scoped Profile authority."""
        denied = _authorization_failure(context, "learner:read")
        if denied is None and not _valid_learner_ref(learner_ref):
            denied = Failure(_error("INVALID_REQUEST", "VALIDATE", "invalid learner ref"))
        details: dict[str, Any] = {"resolution": "MCP_IMPLICIT_CONTENT"}
        if denied is not None:
            return await self._audited(
                denied,
                context=context,
                operation="FEISHU_MCP_LEARNER_CONTENT_RESOLVE",
                resource_type="LEARNER_PROFILE",
                resource_id=learner_ref or "lrn_invalid",
                purpose="TEACHER_SUPPORT",
                details=details,
            )
        loaded = await self._store.learner_content_refs(
            context.actor.tenant_id, learner_ref
        )
        if isinstance(loaded, Failure):
            result: Result[ContentRef] = Failure(loaded.error)
        elif not loaded.value:
            result = Failure(_error("NOT_FOUND", "AUTHORITY", "learner profile not found"))
            details["candidate_count"] = 0
        elif len(loaded.value) != 1:
            result = Failure(
                _error(
                    "INVALID_REQUEST",
                    "AUTHORITY",
                    "learner content is ambiguous; provide content_ref",
                )
            )
            details["candidate_count"] = len(loaded.value)
        else:
            result = Success(loaded.value[0])
            details["candidate_count"] = 1
        return await self._audited(
            result,
            context=context,
            operation="FEISHU_MCP_LEARNER_CONTENT_RESOLVE",
            resource_type="LEARNER_PROFILE",
            resource_id=learner_ref,
            purpose="TEACHER_SUPPORT",
            details=details,
        )

    async def resolve_tenant_content_ref(
        self,
        context: OperationContext,
        class_ref: str,
    ) -> Result[ContentRef]:
        """Resolve a class content only when the tenant Profile cohort is unambiguous."""
        denied = _authorization_failure(context, "class-insights:read")
        if denied is None and not hmac.compare_digest(
            class_ref, stable_class_ref(self._secret, context.actor.tenant_id)
        ):
            denied = Failure(
                _error("AUTHORIZATION_DENIED", "AUTHORITY", "class is outside actor tenant")
            )
        details: dict[str, Any] = {"resolution": "MCP_IMPLICIT_CONTENT"}
        if denied is not None:
            return await self._audited(
                denied,
                context=context,
                operation="FEISHU_MCP_CLASS_CONTENT_RESOLVE",
                resource_type="CLASS_INSIGHTS",
                resource_id=class_ref,
                purpose="TEACHER_PLANNING",
                details=details,
            )
        loaded = await self._store.tenant_content_refs(context.actor.tenant_id)
        if isinstance(loaded, Failure):
            result: Result[ContentRef] = Failure(loaded.error)
        elif not loaded.value:
            result = Failure(
                _error("NOT_FOUND", "AUTHORITY", "tenant has no learner profile content")
            )
            details["candidate_count"] = 0
        elif len(loaded.value) != 1:
            result = Failure(
                _error(
                    "INVALID_REQUEST",
                    "AUTHORITY",
                    "tenant content is ambiguous; provide content_ref",
                )
            )
            details["candidate_count"] = len(loaded.value)
        else:
            result = Success(loaded.value[0])
            details["candidate_count"] = 1
        return await self._audited(
            result,
            context=context,
            operation="FEISHU_MCP_CLASS_CONTENT_RESOLVE",
            resource_type="CLASS_INSIGHTS",
            resource_id=class_ref,
            purpose="TEACHER_PLANNING",
            details=details,
        )

    async def learner_query(
        self,
        body: Mapping[str, Any],
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[Mapping[str, Any]]:
        purpose = _text(body.get("purpose"))
        learner_ref = _text(body.get("learner_ref"))
        consent_basis = _text(body.get("consent_basis"))
        audit_details = _learner_audit_details(body, consent_basis)
        preflight = self._preflight(
            context=context,
            body=body,
            required_role="learner:read",
            purpose=purpose,
            allowed_purposes=LEARNER_PURPOSES,
            idempotency_key=idempotency_key,
        )
        if preflight is None and consent_basis not in LEARNER_CONSENT_BASES:
            preflight = Failure(_error("INVALID_REQUEST", "VALIDATE", "invalid consent basis"))
        if preflight is not None:
            return await self._audited(
                preflight,
                context=context,
                operation="FEISHU_LEARNER_QUERY",
                resource_type="LEARNER_PROFILE",
                resource_id=learner_ref or "lrn_invalid",
                purpose=purpose,
                details=audit_details,
            )
        try:
            occurred_from, occurred_to = _optional_time_range(body.get("time_range"))
        except ValueError as error:
            return await self._audited(
                Failure(_error("INVALID_REQUEST", "VALIDATE", str(error))),
                context=context,
                operation="FEISHU_LEARNER_QUERY",
                resource_type="LEARNER_PROFILE",
                resource_id=learner_ref,
                purpose=purpose,
                details=audit_details,
            )
        loaded = await self._store.learner_bundle(
            context.actor.tenant_id, learner_ref, occurred_from, occurred_to
        )
        if isinstance(loaded, Failure):
            return await self._audited(
                loaded,
                context=context,
                operation="FEISHU_LEARNER_QUERY",
                resource_type="LEARNER_PROFILE",
                resource_id=learner_ref,
                purpose=purpose,
                details=audit_details,
            )
        bundle = loaded.value
        if not _profile_authority_valid(
            bundle.profile, self._secret, context.actor.tenant_id
        ) or not hmac.compare_digest(bundle.profile.learner_ref, learner_ref):
            return await self._audited(
                Failure(_error("INVARIANT_VIOLATION", "AUTHORITY", "learner authority drifted")),
                context=context,
                operation="FEISHU_LEARNER_QUERY",
                resource_type="LEARNER_PROFILE",
                resource_id=learner_ref,
                purpose=purpose,
                details=audit_details,
            )
        if not _content_matches(body, bundle.profile.profile, bundle.profile.content_hash):
            return await self._audited(
                Failure(_error("AUTHORIZATION_DENIED", "AUTHORITY", "content context mismatch")),
                context=context,
                operation="FEISHU_LEARNER_QUERY",
                resource_type="LEARNER_PROFILE",
                resource_id=learner_ref,
                purpose=purpose,
                details=audit_details,
            )
        try:
            payload = self._learner_payload(
                bundle,
                requested_fields=_string_list(body.get("requested_fields")),
                purpose=purpose,
                idempotency_key=idempotency_key,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
                trace_id=context.trace_id,
            )
            result: Result[Mapping[str, Any]] = Success(payload)
        except (TypeError, ValueError) as error:
            result = Failure(_error("INVARIANT_VIOLATION", "PROJECTION", str(error)))
        evidence_ids = (
            tuple(
                item["evidence_id"]
                for item in cast(list[dict[str, Any]], result.value.get("recent_evidence", []))
            )
            if isinstance(result, Success)
            else ()
        )
        return await self._audited(
            result,
            context=context,
            operation="FEISHU_LEARNER_QUERY",
            resource_type="LEARNER_PROFILE",
            resource_id=learner_ref,
            purpose=purpose,
            evidence_ids=evidence_ids[:64],
            details=audit_details,
        )

    async def class_insights(
        self,
        body: Mapping[str, Any],
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[Mapping[str, Any]]:
        purpose = _text(body.get("purpose"))
        class_ref = _text(body.get("class_ref"))
        preflight = self._preflight(
            context=context,
            body=body,
            required_role="class-insights:read",
            purpose=purpose,
            allowed_purposes=CLASS_PURPOSES,
            idempotency_key=idempotency_key,
        )
        if preflight is None and not hmac.compare_digest(
            class_ref, stable_class_ref(self._secret, context.actor.tenant_id)
        ):
            preflight = Failure(
                _error("AUTHORIZATION_DENIED", "AUTHORITY", "class is outside actor tenant")
            )
        if preflight is not None:
            return await self._audited(
                preflight,
                context=context,
                operation="FEISHU_CLASS_INSIGHTS",
                resource_type="CLASS_INSIGHTS",
                resource_id=class_ref or "cls_invalid",
                purpose=purpose,
                details={"dimensions": _string_list(body.get("dimensions"))},
            )
        try:
            occurred_from, occurred_to = _required_time_range(body.get("time_range"))
            content_hash = _body_content_hash(body)
        except ValueError as error:
            return await self._audited(
                Failure(_error("INVALID_REQUEST", "VALIDATE", str(error))),
                context=context,
                operation="FEISHU_CLASS_INSIGHTS",
                resource_type="CLASS_INSIGHTS",
                resource_id=class_ref,
                purpose=purpose,
                details={"dimensions": _string_list(body.get("dimensions"))},
            )
        loaded = await self._store.class_bundles(
            context.actor.tenant_id, content_hash, occurred_from, occurred_to
        )
        if isinstance(loaded, Failure):
            return await self._audited(
                loaded,
                context=context,
                operation="FEISHU_CLASS_INSIGHTS",
                resource_type="CLASS_INSIGHTS",
                resource_id=class_ref,
                purpose=purpose,
                details={"dimensions": _string_list(body.get("dimensions"))},
            )
        if any(
            not _profile_authority_valid(bundle.profile, self._secret, context.actor.tenant_id)
            or not _content_matches(body, bundle.profile.profile, bundle.profile.content_hash)
            for bundle in loaded.value
        ):
            return await self._audited(
                Failure(_error("AUTHORIZATION_DENIED", "AUTHORITY", "content context mismatch")),
                context=context,
                operation="FEISHU_CLASS_INSIGHTS",
                resource_type="CLASS_INSIGHTS",
                resource_id=class_ref,
                purpose=purpose,
                details={"dimensions": _string_list(body.get("dimensions"))},
            )
        try:
            payload = self._class_payload(
                loaded.value,
                body=body,
                class_ref=class_ref,
                purpose=purpose,
                idempotency_key=idempotency_key,
                trace_id=context.trace_id,
            )
            result = Success(payload)
        except (TypeError, ValueError) as error:
            result = Failure(_error("INVARIANT_VIOLATION", "PROJECTION", str(error)))
        return await self._audited(
            result,
            context=context,
            operation="FEISHU_CLASS_INSIGHTS",
            resource_type="CLASS_INSIGHTS",
            resource_id=class_ref,
            purpose=purpose,
            details={"dimensions": _string_list(body.get("dimensions"))},
        )

    async def redacted_evidence(
        self,
        evidence_id: str,
        purpose: str,
        context: OperationContext,
    ) -> Result[Mapping[str, Any]]:
        denied = _authorization_failure(context, "evidence:read")
        if denied is None and purpose not in EVIDENCE_PURPOSES:
            denied = Failure(_error("INVALID_REQUEST", "VALIDATE", "unsupported purpose"))
        if denied is None and not _valid_evidence_id(evidence_id):
            denied = Failure(_error("INVALID_REQUEST", "VALIDATE", "invalid evidence id"))
        if denied is not None:
            return await self._audited(
                denied,
                context=context,
                operation="FEISHU_EVIDENCE_VIEW",
                resource_type="EVIDENCE",
                resource_id=evidence_id,
                purpose=purpose,
                evidence_ids=(evidence_id,) if _valid_evidence_id(evidence_id) else (),
                details={"redaction_policy": "FEISHU_EVIDENCE_V1"},
            )
        loaded = await self._store.evidence_bundle(context.actor.tenant_id, evidence_id)
        if isinstance(loaded, Failure):
            return await self._audited(
                loaded,
                context=context,
                operation="FEISHU_EVIDENCE_VIEW",
                resource_type="EVIDENCE",
                resource_id=evidence_id,
                purpose=purpose,
                details={"redaction_policy": "FEISHU_EVIDENCE_V1"},
            )
        if (
            not _profile_authority_valid(
                loaded.value.profile, self._secret, context.actor.tenant_id
            )
            or loaded.value.projection.learner_id != loaded.value.profile.learner_id
        ):
            return await self._audited(
                Failure(_error("INVARIANT_VIOLATION", "AUTHORITY", "evidence subject drifted")),
                context=context,
                operation="FEISHU_EVIDENCE_VIEW",
                resource_type="EVIDENCE",
                resource_id=evidence_id,
                purpose=purpose,
                evidence_ids=(evidence_id,),
                details={"redaction_policy": "FEISHU_EVIDENCE_V1"},
            )
        try:
            payload = self._evidence_payload(loaded.value, purpose, context.trace_id)
            result = Success(payload)
        except (TypeError, ValueError) as error:
            result = Failure(_error("INVARIANT_VIOLATION", "REDACTION", str(error)))
        return await self._audited(
            result,
            context=context,
            operation="FEISHU_EVIDENCE_VIEW",
            resource_type="EVIDENCE",
            resource_id=evidence_id,
            purpose=purpose,
            evidence_ids=(evidence_id,),
            details={"redaction_policy": "FEISHU_EVIDENCE_V1"},
        )

    def _preflight(
        self,
        *,
        context: OperationContext,
        body: Mapping[str, Any],
        required_role: str,
        purpose: str,
        allowed_purposes: frozenset[str],
        idempotency_key: str,
    ) -> Failure | None:
        denied = _authorization_failure(context, required_role)
        if denied is not None:
            return denied
        if purpose not in allowed_purposes:
            return Failure(_error("INVALID_REQUEST", "VALIDATE", "unsupported purpose"))
        if IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            return Failure(_error("INVALID_REQUEST", "VALIDATE", "invalid Idempotency-Key"))
        if not _body_context_matches(body, context):
            return Failure(
                _error("AUTHORIZATION_DENIED", "AUTHORITY", "request body context mismatch")
            )
        return None

    def _learner_payload(
        self,
        bundle: LearnerLearningBundle,
        *,
        requested_fields: list[str],
        purpose: str,
        idempotency_key: str,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
        trace_id: str,
    ) -> dict[str, Any]:
        now = _aware(self._clock(), "clock")
        profile = bundle.profile.profile
        projected_at = _profile_updated_at(profile, bundle.profile.updated_at)
        lag = max(0, int((now - projected_at).total_seconds()))
        result: dict[str, Any] = {
            "query_id": stable_query_ref(
                self._secret,
                "lqry",
                bundle.profile.tenant_id,
                idempotency_key,
                bundle.profile.learner_ref,
                purpose,
            ),
            "learner_ref": bundle.profile.learner_ref,
            "as_of": _timestamp(now),
            "data_freshness": {
                "projected_through_event_at": _timestamp(projected_at),
                "projection_lag_seconds": lag,
                "is_stale": lag > 86_400,
            },
            "redaction": {
                "direct_identifiers_removed": True,
                "fields_omitted": list(REDACTED_FIELDS),
            },
            "trace_id": trace_id,
        }
        if "MASTERY_SUMMARY" in requested_fields:
            result["mastery_summary"] = _mastery_summary(bundle.profile, now, purpose)
        if "RECENT_EVIDENCE" in requested_fields:
            result["recent_evidence"] = _recent_evidence(
                bundle.profile, purpose, occurred_from, occurred_to
            )
        if "ACTIVITY_SUMMARY" in requested_fields:
            result["activity_summary"] = {
                "sessions": len({item.session_id for item in bundle.projections}),
                "completed_tasks": sum(_task_success(item) for item in bundle.projections),
            }
        if "SUPPORT_NEEDS" in requested_fields:
            result["support_needs"] = _support_needs(bundle, now)
        if "RECOMMENDED_NEXT_STEPS" in requested_fields:
            result["recommended_next_steps"] = _recommended_next_steps(bundle, now)
        return result

    def _class_payload(
        self,
        bundles: Sequence[LearnerLearningBundle],
        *,
        body: Mapping[str, Any],
        class_ref: str,
        purpose: str,
        idempotency_key: str,
        trace_id: str,
    ) -> dict[str, Any]:
        privacy = _mapping(body.get("privacy"), "privacy")
        requested_minimum = _integer(privacy.get("minimum_cohort_size"), "minimum_cohort_size")
        effective_minimum = max(requested_minimum, self._minimum_cohort_size)
        dimensions = _string_list(body.get("dimensions"))
        now = _aware(self._clock(), "clock")
        counts: dict[tuple[str, str], set[str]] = defaultdict(set)
        for bundle in bundles:
            learner_ref = bundle.profile.learner_ref
            task_concepts = {
                _task_concept(item)
                for item in bundle.projections
                if _task_concept(item) is not None
            }
            if "CONCEPT_MASTERY" in dimensions:
                competencies = _mapping_or_empty(bundle.profile.profile.get("competencies"))
                for concept in sorted(cast(set[str], task_concepts)):
                    competency = competencies.get(concept)
                    if isinstance(competency, Mapping):
                        state = _public_stage(competency, now)
                        counts[("CONCEPT_MASTERY", f"{concept}|{state}")].add(learner_ref)
            if "COMMON_ERRORS" in dimensions:
                for item in bundle.projections:
                    error = _main_error(item)
                    if error != "NONE":
                        counts[("COMMON_ERRORS", error)].add(learner_ref)
            if "SUPPORT_NEEDS" in dimensions:
                for code in _support_codes(bundle, now):
                    counts[("SUPPORT_NEEDS", code)].add(learner_ref)
            if "ENGAGEMENT" in dimensions:
                key = "ACTIVE" if bundle.projections else "NO_ACTIVITY"
                counts[("ENGAGEMENT", key)].add(learner_ref)
            if "COMPLETION" in dimensions:
                key = (
                    "ANY_TASK_COMPLETED"
                    if any(_task_success(item) for item in bundle.projections)
                    else "NO_TASK_COMPLETED"
                )
                counts[("COMPLETION", key)].add(learner_ref)
        cohort_size = len(bundles)
        insights = [
            _privacy_cell(dimension, key, len(learners), cohort_size, effective_minimum)
            for (dimension, key), learners in sorted(counts.items())
        ][:500]
        return {
            "query_id": stable_query_ref(
                self._secret,
                "ciq",
                bundles[0].profile.tenant_id if bundles else class_ref,
                idempotency_key,
                class_ref,
                purpose,
            ),
            "class_ref": class_ref,
            "as_of": _timestamp(now),
            "cohort_size": cohort_size,
            "privacy": {
                "minimum_cohort_size": requested_minimum,
                "effective_minimum_cohort_size": effective_minimum,
                "policy_version": "FEISHU_AGGREGATION_V1",
                "small_cells_suppressed": True,
                "contains_learner_identifiers": False,
            },
            "insights": insights,
            "trace_id": trace_id,
        }

    def _evidence_payload(
        self, bundle: EvidenceLearningBundle, purpose: str, trace_id: str
    ) -> dict[str, Any]:
        return redact_evidence_for_feishu(
            bundle,
            purpose=purpose,
            trace_id=trace_id,
            observed_now=_aware(self._clock(), "clock"),
        )

    async def _audited(
        self,
        result: Result[_ResultValue],
        *,
        context: OperationContext,
        operation: str,
        resource_type: str,
        resource_id: str,
        purpose: str | None,
        details: Mapping[str, Any],
        evidence_ids: tuple[str, ...] = (),
    ) -> Result[_ResultValue]:
        failure = result.error if isinstance(result, Failure) else None
        outcome: Literal["ALLOWED", "DENIED", "FAILED"] = "ALLOWED"
        if failure is not None:
            outcome = "DENIED" if failure.code == "AUTHORIZATION_DENIED" else "FAILED"
        audit = await self._store.append_access_audit(
            context=context,
            operation=operation,
            outcome=outcome,
            resource_type=resource_type,
            resource_id=resource_id[:256] or "invalid",
            purpose=purpose if purpose and purpose.isupper() else None,
            evidence_ids=evidence_ids,
            error_code=failure.code if failure is not None else None,
            details=details,
        )
        if isinstance(audit, Failure):
            return Failure(audit.error)
        return result


def redact_evidence_for_feishu(
    bundle: EvidenceLearningBundle,
    *,
    purpose: str,
    trace_id: str,
    observed_now: datetime,
) -> dict[str, Any]:
    """Build the single released evidence projection used by API and Base sync."""
    if purpose not in EVIDENCE_PURPOSES:
        raise ValueError("unsupported evidence purpose")
    document = bundle.evidence.document
    evidence_ref = _evidence_ref(document.get("evidence_ref"), purpose)
    if evidence_ref["evidence_id"] != bundle.evidence.evidence_id:
        raise ValueError("Evidence identity drifted")
    evidence_kind = _evidence_kind(document)
    projection = bundle.projection
    projected = projection_facts_for_feishu(
        bundle.profile,
        projection,
        bundle.learner_projections,
        observed_now=observed_now,
    )
    fact_values: dict[str, str | int | bool] = {
        "evidence_kind": evidence_kind,
        **projected,
    }
    if set(fact_values) != EVIDENCE_FACT_NAMES:
        raise ValueError("Evidence facts escaped the released whitelist")
    observed_at = _document_time(document, "occurred_at", bundle.evidence.recorded_at)
    return {
        "evidence_ref": evidence_ref,
        "learner_ref": bundle.profile.learner_ref,
        "observed_at": _timestamp(observed_at),
        "summary": _evidence_summary(
            str(projected["task_result"]),
            str(projected["task_ref"]),
            str(projected["main_error"]),
        ),
        "facts": [{"name": name, "value": fact_values[name]} for name in sorted(fact_values)],
        "provenance": {
            "event_id": _source_event_id(projection),
            "command_id": projection.command_id,
            "source_module": _source_module(evidence_kind),
        },
        "redaction": {
            "direct_identifiers_removed": True,
            "policy_version": "FEISHU_EVIDENCE_V1",
            "fields_omitted": list(REDACTED_FIELDS),
        },
        "trace_id": trace_id,
    }


def projection_facts_for_feishu(
    profile: LearnerProfileAuthority,
    projection: LearningProjectionAuthority,
    history: Sequence[LearningProjectionAuthority],
    *,
    observed_now: datetime,
) -> dict[str, str | int | bool]:
    """Project the shared, non-identifying learning facts used by INT3 surfaces."""
    task_ref = _task_id(projection)
    if task_ref is None:
        raise ValueError("Evidence task authority is missing")
    task_result = "COMPLETED" if _task_success(projection) else "NOT_COMPLETED"
    assistance_level, skill_patch_used = _assistance(projection)
    concept = _task_concept(projection)
    stage = "NOT_OBSERVED"
    if concept is not None:
        competency = _frozen_projection_competency(projection, concept)
        if isinstance(competency, Mapping):
            # A daily/evidence record is historical. Derive the public stage at
            # the immutable projection time, never from the mutable Profile head
            # or from today's review clock.
            stage = _public_stage(competency, _aware(projection.completed_at, "completed_at"))
    attempts = sum(
        1
        for item in history
        if item.session_id == projection.session_id
        and _task_id(item) == task_ref
        and item.through_sequence <= projection.through_sequence
    )
    return {
        "task_ref": task_ref,
        "run_ref": projection.run_id,
        "task_result": task_result,
        "attempt_count": attempts,
        "main_error": _main_error(projection),
        "ai_assistance_level": assistance_level,
        "skill_patch_used": skill_patch_used,
        "knowledge_stage": stage,
    }


def _frozen_projection_competency(
    projection: LearningProjectionAuthority,
    concept: str,
) -> Mapping[str, Any] | None:
    """Read a competency only from this projection's immutable commit receipt.

    Older projections may not carry the snapshot.  That is an honest absence,
    not permission to substitute the current learner Profile as historical fact.
    Once a frozen snapshot is present, malformed bytes or hashes fail closed.
    """

    projection_receipt = projection.result.get("projection_receipt")
    if projection_receipt is None:
        return None
    if not isinstance(projection_receipt, Mapping):
        raise ValueError("frozen projection receipt is malformed")
    commit = projection_receipt.get("receipt_json")
    if commit is None:
        return None
    if not isinstance(commit, Mapping):
        raise ValueError("frozen projection commit is malformed")
    learner = commit.get("learner")
    if learner is None:
        return None
    if not isinstance(learner, Mapping):
        raise ValueError("frozen learner authority is malformed")
    frozen_profile = learner.get("profile")
    if frozen_profile is None:
        return None
    if not isinstance(frozen_profile, Mapping):
        raise ValueError("frozen learner profile is malformed")
    profile_sha256 = learner.get("profile_sha256")
    if not isinstance(profile_sha256, str) or profile_sha256 != canonical_json_sha256(
        frozen_profile
    ):
        raise ValueError("frozen learner profile hash drifted")
    competencies = frozen_profile.get("competencies")
    if competencies is None:
        return None
    if not isinstance(competencies, Mapping):
        raise ValueError("frozen learner competencies are malformed")
    competency = competencies.get(concept)
    if competency is None:
        return None
    if not isinstance(competency, Mapping):
        raise ValueError("frozen learner competency is malformed")
    frozen_concept = competency.get("concept")
    if frozen_concept is not None and frozen_concept != concept:
        raise ValueError("frozen learner competency concept drifted")
    return competency


def previous_knowledge_stage_for_feishu(
    projection: LearningProjectionAuthority,
    history: Sequence[LearningProjectionAuthority],
) -> str | None:
    """Return the prior same-concept stage only from an immutable receipt."""
    concept = _task_concept(projection)
    if concept is None:
        return None
    candidates = [
        item
        for item in history
        if item.learner_id == projection.learner_id
        and item.through_sequence < projection.through_sequence
        and _task_concept(item) == concept
    ]
    if not candidates:
        return None
    previous = max(candidates, key=lambda item: (item.through_sequence, item.job_id))
    competency = _frozen_projection_competency(previous, concept)
    if competency is None:
        return None
    return _public_stage(
        competency,
        _aware(previous.completed_at, "previous.completed_at"),
    )


def stable_learner_ref(secret: str, tenant_id: str, learner_id: str) -> str:
    return _opaque_ref(secret, "lrn", tenant_id, learner_id)


def stable_class_ref(secret: str, tenant_id: str) -> str:
    return _opaque_ref(secret, "cls", tenant_id)


def stable_query_ref(secret: str, prefix: str, *parts: str) -> str:
    if prefix not in {"lqry", "ciq"}:
        raise ValueError("unsupported query reference prefix")
    return _opaque_ref(secret, prefix, *parts)


def _opaque_ref(secret: str, prefix: str, *parts: str) -> str:
    message = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{prefix}_{encoded}"


def _authorization_failure(context: OperationContext, required_role: str) -> Failure | None:
    if context.actor.actor_type.value not in ALLOWED_ACTOR_TYPES:
        return Failure(
            _error("AUTHORIZATION_DENIED", "AUTHORITY", "actor type cannot read teacher data")
        )
    if required_role not in context.actor.roles:
        return Failure(_error("AUTHORIZATION_DENIED", "AUTHORITY", "required role is missing"))
    return None


def _body_context_matches(body: Mapping[str, Any], context: OperationContext) -> bool:
    supplied = body.get("context")
    if not isinstance(supplied, Mapping):
        return False
    actor = supplied.get("actor")
    if not isinstance(actor, Mapping):
        return False
    expected_actor = {
        "tenant_id": context.actor.tenant_id,
        "actor_id": context.actor.actor_id,
        "actor_type": context.actor.actor_type.value,
        "roles": list(context.actor.roles),
    }
    if actor != expected_actor:
        return False
    expected = {
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "schema_version": context.schema_version,
    }
    if any(supplied.get(key) != value for key, value in expected.items()):
        return False
    try:
        _parse_timestamp(supplied.get("requested_at"), "context.requested_at")
    except ValueError:
        return False
    return isinstance(supplied.get("content_ref"), Mapping)


def _content_matches(
    body: Mapping[str, Any], profile: Mapping[str, Any], content_hash: str
) -> bool:
    supplied = _mapping_or_empty(_mapping_or_empty(body.get("context")).get("content_ref"))
    authority = _mapping_or_empty(profile.get("content"))
    if supplied.get("content_hash") != content_hash:
        return False
    return not authority or supplied == authority


def _profile_authority_valid(profile: LearnerProfileAuthority, secret: str, tenant_id: str) -> bool:
    authority = profile.profile
    content = _mapping_or_empty(authority.get("content"))
    return (
        profile.tenant_id == tenant_id
        and authority.get("learner_id") == profile.learner_id
        and authority.get("actor_id") == profile.actor_id
        and content.get("content_hash") == profile.content_hash
        and hmac.compare_digest(
            profile.learner_ref,
            stable_learner_ref(secret, tenant_id, profile.learner_id),
        )
    )


def _body_content_hash(body: Mapping[str, Any]) -> str:
    context = _mapping(body.get("context"), "context")
    content = _mapping(context.get("content_ref"), "content_ref")
    value = content.get("content_hash")
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("content_ref.content_hash is invalid")
    return value


def _optional_time_range(value: Any) -> tuple[datetime | None, datetime | None]:
    if value is None:
        return None, None
    return _required_time_range(value)


def _required_time_range(value: Any) -> tuple[datetime, datetime]:
    time_range = _mapping(value, "time_range")
    occurred_from = _parse_timestamp(time_range.get("from"), "time_range.from")
    occurred_to = _parse_timestamp(time_range.get("to"), "time_range.to")
    if occurred_from > occurred_to:
        raise ValueError("time_range.from must not be after time_range.to")
    return occurred_from, occurred_to


def _mastery_summary(
    profile: LearnerProfileAuthority, now: datetime, purpose: str
) -> list[dict[str, Any]]:
    competencies = _mapping_or_empty(profile.profile.get("competencies"))
    catalog = {
        item["evidence_id"]: item
        for item in _profile_evidence(profile.profile)
        if isinstance(item.get("evidence_id"), str)
    }
    mastery: list[dict[str, Any]] = []
    for concept, raw in sorted(competencies.items()):
        if not isinstance(concept, str) or not isinstance(raw, Mapping):
            raise ValueError("Learner competency authority is malformed")
        evidence_ids = raw.get("evidence_ids", [])
        if not isinstance(evidence_ids, list):
            raise ValueError("Learner competency evidence_ids is malformed")
        references = [
            _evidence_ref(catalog[evidence_id], purpose)
            for evidence_id in evidence_ids
            if isinstance(evidence_id, str) and evidence_id in catalog
        ]
        mastery.append(
            {
                "concept_ref": concept[:256],
                "state": _public_stage(raw, now),
                # Projection state is deterministically derived from authoritative bytes.
                # Integer 1 also preserves the locked canonical-JSON no-fraction rule.
                "confidence": 1,
                "evidence_refs": references[:100],
                "updated_at": _timestamp(
                    _document_time(raw, "last_observed_at", profile.updated_at)
                ),
            }
        )
    return mastery[:500]


def _recent_evidence(
    profile: LearnerProfileAuthority,
    purpose: str,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
) -> list[dict[str, Any]]:
    result: list[tuple[datetime, dict[str, Any]]] = []
    for raw in _profile_evidence(profile.profile):
        reference = _evidence_ref(raw, purpose)
        created_at = _parse_timestamp(reference["created_at"], "evidence.created_at")
        if occurred_from is not None and created_at < occurred_from:
            continue
        if occurred_to is not None and created_at > occurred_to:
            continue
        result.append((created_at, reference))
    return [item for _, item in sorted(result, key=lambda pair: pair[0], reverse=True)[:200]]


def _profile_evidence(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = profile.get("evidence_refs", [])
    if not isinstance(value, list):
        raise ValueError("Learner evidence catalog is malformed")
    if len(value) > _MAX_QUERY_ROWS:
        raise ValueError("Learner evidence catalog exceeds query safety bound")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError("Learner evidence catalog entry is malformed")
    return cast(list[Mapping[str, Any]], value)


def _evidence_ref(value: Any, purpose: str) -> dict[str, Any]:
    reference = _mapping(value, "evidence_ref")
    evidence_id = reference.get("evidence_id")
    evidence_type = reference.get("evidence_type")
    created_at = reference.get("created_at")
    if not _valid_evidence_id(evidence_id):
        raise ValueError("Evidence reference id is invalid")
    if evidence_type not in _EVIDENCE_TYPES or not isinstance(created_at, str):
        raise ValueError("Evidence reference metadata is invalid")
    result: dict[str, Any] = {
        "evidence_id": evidence_id,
        "evidence_type": evidence_type,
        "created_at": _timestamp(_parse_timestamp(created_at, "evidence.created_at")),
        "uri": f"/integrations/feishu/v1/evidence/{evidence_id}?purpose={purpose}",
    }
    digest = reference.get("sha256")
    if isinstance(digest, str):
        if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("Evidence reference digest is invalid")
        result["sha256"] = digest
    return result


def _public_stage(competency: Mapping[str, Any], now: datetime) -> str:
    review_at = competency.get("next_review_at")
    if isinstance(review_at, str) and _parse_timestamp(review_at, "next_review_at") <= now:
        return "REVIEW_NEEDED"
    stage = {
        "OBSERVED": "EMERGING",
        "DEMONSTRATED": "DEVELOPING",
        "RETAINED": "PROFICIENT",
        "TRANSFERRED": "PROFICIENT",
    }.get(_text(competency.get("evidence_stage")))
    if stage is None:
        raise ValueError("Learner evidence stage is invalid")
    return stage


def _support_needs(bundle: LearnerLearningBundle, now: datetime) -> list[str]:
    needs: list[str] = []
    competencies = _mapping_or_empty(bundle.profile.profile.get("competencies"))
    for concept, value in sorted(competencies.items()):
        if isinstance(concept, str) and isinstance(value, Mapping):
            if _public_stage(value, now) == "REVIEW_NEEDED":
                needs.append(f"{concept[:220]}：已到复习时间")
            assistance = value.get("assistance_level")
            if isinstance(assistance, int) and not isinstance(assistance, bool) and assistance >= 4:
                needs.append(f"{concept[:220]}：需要逐步降低 AI 辅助")
    for task in sorted(_repeated_failure_tasks(bundle)):
        needs.append(f"{task[:220]}：连续尝试尚未完成")
    return list(dict.fromkeys(needs))[:100]


def _recommended_next_steps(bundle: LearnerLearningBundle, now: datetime) -> list[str]:
    steps: list[str] = []
    competencies = _mapping_or_empty(bundle.profile.profile.get("competencies"))
    for concept, value in sorted(competencies.items()):
        if not isinstance(concept, str) or not isinstance(value, Mapping):
            continue
        state = _public_stage(value, now)
        if state == "REVIEW_NEEDED":
            steps.append(f"复习知识点 {concept[:220]}，并安排一次低辅助练习。")
        elif state in {"EMERGING", "DEVELOPING"}:
            steps.append(f"继续练习知识点 {concept[:220]}，优先让学生独立解释思路。")
    for task in sorted(_repeated_failure_tasks(bundle)):
        steps.append(f"针对任务 {task[:220]} 讲解主要错误后再进行一次独立尝试。")
    if not steps and bundle.projections:
        steps.append("保持当前节奏，并在下一次任务中检查知识迁移。")
    return list(dict.fromkeys(steps))[:20]


def _support_codes(bundle: LearnerLearningBundle, now: datetime) -> set[str]:
    codes: set[str] = set()
    competencies = _mapping_or_empty(bundle.profile.profile.get("competencies"))
    if any(
        isinstance(value, Mapping) and _public_stage(value, now) == "REVIEW_NEEDED"
        for value in competencies.values()
    ):
        codes.add("REVIEW_DUE")
    if _repeated_failure_tasks(bundle):
        codes.add("REPEATED_FAILURE")
    if any(_assistance(item)[0] >= 4 for item in bundle.projections):
        codes.add("AI_ASSISTANCE_USED")
    if any(_assistance(item)[1] for item in bundle.projections):
        codes.add("SKILL_PATCH_USED")
    return codes


def _repeated_failure_tasks(bundle: LearnerLearningBundle) -> set[str]:
    counts: dict[str, int] = defaultdict(int)
    for item in bundle.projections:
        task = _task_id(item)
        if task is not None and not _task_success(item):
            counts[task] += 1
    return {task for task, count in counts.items() if count >= 2}


def _privacy_cell(
    dimension: str, key: str, count: int, cohort_size: int, threshold: int
) -> dict[str, Any]:
    suppressed = cohort_size < threshold or count < threshold
    return {
        "dimension": dimension,
        "key": key[:256],
        "learner_count": None if suppressed else count,
        "ratio": None if suppressed else count / cohort_size,
        "suppressed": suppressed,
    }


def _learner_audit_details(body: Mapping[str, Any], consent_basis: str) -> dict[str, Any]:
    """Retain only bounded contract enums in learner-query audit details."""
    return {
        "requested_fields": _string_list(body.get("requested_fields")),
        "consent_basis": (
            consent_basis if consent_basis in LEARNER_CONSENT_BASES else "INVALID_OR_MISSING"
        ),
    }


def _task_success(item: LearningProjectionAuthority) -> bool:
    run = _mapping_or_empty(item.projection.get("run"))
    return run.get("task_success") is True


def _task_id(item: LearningProjectionAuthority) -> str | None:
    task = _mapping_or_empty(item.projection.get("task"))
    value = task.get("task_id")
    return value if isinstance(value, str) and value else None


def _task_concept(item: LearningProjectionAuthority) -> str | None:
    task = _mapping_or_empty(item.projection.get("task"))
    value = task.get("concept")
    return value if isinstance(value, str) and value else None


def _main_error(item: LearningProjectionAuthority) -> str:
    run = _mapping_or_empty(item.projection.get("run"))
    value = run.get("failure_key")
    return value[:128] if isinstance(value, str) and value else "NONE"


def _assistance(item: LearningProjectionAuthority) -> tuple[int, bool]:
    assistance = _mapping_or_empty(item.projection.get("assistance"))
    used_patch = assistance.get("used_skill_patch") is True
    return (4 if used_patch else 0), used_patch


def _evidence_kind(document: Mapping[str, Any]) -> str:
    payload = _mapping_or_empty(document.get("payload"))
    value = payload.get("evidence_kind")
    if not isinstance(value, str) or not value:
        raise ValueError("Evidence kind is missing")
    return value


def _source_event_id(item: LearningProjectionAuthority) -> str:
    value = item.projection.get("source_feedback_event_id")
    if isinstance(value, str) and 1 <= len(value) <= 128:
        return value
    if 1 <= len(item.source_event_id) <= 128:
        return item.source_event_id
    raise ValueError("Evidence source event id is invalid")


def _source_module(evidence_kind: str) -> str:
    return {
        "WORLD_COMMIT": "WORLD_ENGINE",
        "LEARNER_OBSERVATION": "LEARNER_PROJECTION",
        "POLICY_DECISION": "SKILL_LIFECYCLE",
        "TEST_REPORT": "SKILL_LIFECYCLE",
    }.get(evidence_kind, "AGENT_RUNTIME")


def _evidence_summary(task_result: str, task_ref: str, main_error: str) -> str:
    if task_result == "COMPLETED":
        return f"任务 {task_ref} 已完成；证据仅展示脱敏学习事实。"
    return f"任务 {task_ref} 尚未完成；主要错误为 {main_error}。"


def _profile_updated_at(profile: Mapping[str, Any], fallback: datetime) -> datetime:
    return _document_time(profile, "updated_at", fallback)


def _document_time(value: Mapping[str, Any], key: str, fallback: datetime) -> datetime:
    raw = value.get(key)
    return _parse_timestamp(raw, key) if isinstance(raw, str) else _aware(fallback, key)


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a date-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be a date-time") from error
    return _aware(parsed, field)


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _aware(value, "timestamp").isoformat().replace("+00:00", "Z")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _valid_evidence_id(value: Any) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"evidence_[A-Za-z0-9_-]{8,128}", value) is not None
    )


def _valid_learner_ref(value: Any) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"lrn_[A-Za-z0-9_-]{8,128}", value) is not None
    )


def _error(code: str, stage: str, message: str) -> ContractError:
    metadata = {
        "AUTHORIZATION_DENIED": (ErrorCategory.AUTHORIZATION, "auth.permission_denied"),
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, "request.invalid"),
        "NOT_FOUND": (ErrorCategory.VALIDATION, "resource.not_found"),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=False,
        user_message_key=metadata[1],
        stage=stage,
        message=message[:512] or code,
    )


__all__ = [
    "EVIDENCE_FACT_NAMES",
    "EvidenceAuthority",
    "EvidenceLearningBundle",
    "FeishuLearningQueries",
    "FeishuLearningStore",
    "LearnerLearningBundle",
    "LearnerProfileAuthority",
    "LearningProjectionAuthority",
    "projection_facts_for_feishu",
    "previous_knowledge_stage_for_feishu",
    "redact_evidence_for_feishu",
    "stable_class_ref",
    "stable_learner_ref",
]
