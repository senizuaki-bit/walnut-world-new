"""Unit closure for the read-only Feishu learning projections."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    ContractError,
    ErrorCategory,
    Failure,
    OperationContext,
    Success,
    canonical_json_sha256,
)

from walnut_backend.api.app import create_app
from walnut_backend.api.middleware import MOCK_TEACHER_ROLES
from walnut_backend.api.response_validation import canonical_payload
from walnut_backend.application.feishu.learning_queries import (
    EVIDENCE_FACT_NAMES,
    EvidenceAuthority,
    EvidenceLearningBundle,
    FeishuLearningQueries,
    LearnerLearningBundle,
    LearnerProfileAuthority,
    LearningProjectionAuthority,
    projection_facts_for_feishu,
    stable_class_ref,
    stable_learner_ref,
)
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, ContractRelease, Settings

NOW = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)
SECRET = "feishu-query-test-secret-" + "x" * 32
TENANT = "tenant_feishu_test"
CONTENT_HASH = "a" * 64
TEACHER_ROLES = (
    "class-insights:read",
    "evidence:read",
    "learner:read",
    "teacher",
)


class FakeStore:
    def __init__(
        self,
        learner: LearnerLearningBundle,
        *,
        class_bundles: tuple[LearnerLearningBundle, ...] | None = None,
        evidence: EvidenceLearningBundle | None = None,
    ) -> None:
        self.learner = learner
        self.class_value = class_bundles or (learner,)
        self.evidence = evidence
        self.audits: list[dict[str, Any]] = []
        self.learner_reads = 0
        self.class_reads = 0
        self.evidence_reads = 0
        content = learner.profile.profile["content"]
        self.learner_content_values = (ContentRef(**content),)
        self.tenant_content_values = (ContentRef(**content),)
        self.learner_content_reads = 0
        self.tenant_content_reads = 0

    async def learner_content_refs(self, tenant_id, learner_ref):
        self.learner_content_reads += 1
        assert tenant_id == TENANT
        if learner_ref != self.learner.profile.learner_ref:
            return Success(())
        return Success(self.learner_content_values)

    async def tenant_content_refs(self, tenant_id):
        self.tenant_content_reads += 1
        assert tenant_id == TENANT
        return Success(self.tenant_content_values)

    async def learner_bundle(self, tenant_id, learner_ref, occurred_from, occurred_to):
        self.learner_reads += 1
        if tenant_id != self.learner.profile.tenant_id:
            raise AssertionError("tenant filter was not propagated")
        if learner_ref != self.learner.profile.learner_ref:
            return Failure(_not_found())
        return Success(self.learner)

    async def class_bundles(self, tenant_id, content_hash, occurred_from, occurred_to):
        self.class_reads += 1
        assert tenant_id == TENANT
        assert content_hash == CONTENT_HASH
        assert occurred_from <= occurred_to
        return Success(self.class_value)

    async def evidence_bundle(self, tenant_id, evidence_id):
        self.evidence_reads += 1
        if self.evidence is None or self.evidence.evidence.evidence_id != evidence_id:
            return Failure(_not_found())
        assert tenant_id == TENANT
        return Success(self.evidence)

    async def append_access_audit(self, **values):
        self.audits.append(values)
        return Success(None)


class AuditFailingStore(FakeStore):
    async def append_access_audit(self, **values):
        del values
        return Failure(
            ContractError(
                "INTERNAL_ERROR",
                ErrorCategory.INTERNAL,
                False,
                "system.internal_error",
                "AUDIT",
                "audit unavailable",
            )
        )


def test_mcp_content_resolution_is_authoritative_unambiguous_and_audited() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)

    learner = asyncio.run(
        queries.resolve_learner_content_ref(bundle.profile.learner_ref, _context())
    )
    class_content = asyncio.run(
        queries.resolve_tenant_content_ref(
            _context(), stable_class_ref(SECRET, TENANT)
        )
    )

    assert isinstance(learner, Success)
    assert isinstance(class_content, Success)
    assert learner.value == class_content.value == ContentRef(
        "UNIT_FEISHU", "1.0.0", CONTENT_HASH
    )
    assert store.learner_content_reads == store.tenant_content_reads == 1
    assert [item["outcome"] for item in store.audits] == ["ALLOWED", "ALLOWED"]
    assert [item["operation"] for item in store.audits] == [
        "FEISHU_MCP_LEARNER_CONTENT_RESOLVE",
        "FEISHU_MCP_CLASS_CONTENT_RESOLVE",
    ]
    assert all(item["details"]["candidate_count"] == 1 for item in store.audits)


def test_mcp_content_resolution_rejects_empty_and_ambiguous_profile_authority() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    store.learner_content_values = ()
    store.tenant_content_values = (
        ContentRef("UNIT_FEISHU", "1.0.0", CONTENT_HASH),
        ContentRef("UNIT_FEISHU_OTHER", "2.0.0", "b" * 64),
    )

    missing = asyncio.run(
        queries.resolve_learner_content_ref(bundle.profile.learner_ref, _context())
    )
    ambiguous = asyncio.run(
        queries.resolve_tenant_content_ref(
            _context(), stable_class_ref(SECRET, TENANT)
        )
    )

    assert isinstance(missing, Failure) and missing.error.code == "NOT_FOUND"
    assert isinstance(ambiguous, Failure) and ambiguous.error.code == "INVALID_REQUEST"
    assert [item["outcome"] for item in store.audits] == ["FAILED", "FAILED"]
    assert [item["details"]["candidate_count"] for item in store.audits] == [0, 2]


def test_mcp_content_resolution_denies_student_and_cross_tenant_class_before_reads() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    student_context = replace(
        _context(),
        actor=ActorRef(TENANT, "student_actor", ActorType.STUDENT, TEACHER_ROLES),
    )

    student = asyncio.run(
        queries.resolve_learner_content_ref(bundle.profile.learner_ref, student_context)
    )
    cross_tenant = asyncio.run(
        queries.resolve_tenant_content_ref(
            _context(), stable_class_ref(SECRET, "tenant_outside_scope")
        )
    )

    assert isinstance(student, Failure) and student.error.code == "AUTHORIZATION_DENIED"
    assert isinstance(cross_tenant, Failure)
    assert cross_tenant.error.code == "AUTHORIZATION_DENIED"
    assert store.learner_content_reads == store.tenant_content_reads == 0
    assert [item["outcome"] for item in store.audits] == ["DENIED", "DENIED"]


def test_mcp_content_resolution_never_releases_authority_when_audit_fails() -> None:
    bundle = _bundle("learner_01")
    store = AuditFailingStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)

    result = asyncio.run(
        queries.resolve_learner_content_ref(bundle.profile.learner_ref, _context())
    )

    assert isinstance(result, Failure)
    assert result.error.code == "INTERNAL_ERROR"
    assert result.error.stage == "AUDIT"
    assert store.learner_content_reads == 1


def test_learner_query_projects_real_authority_and_audits() -> None:
    bundle = _bundle("learner_01", failures=2, used_patch=True)
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    body = _learner_body(_context(), bundle.profile.learner_ref)

    result = asyncio.run(queries.learner_query(body, "idem_feishu_learner_0001", _context()))

    assert isinstance(result, Success)
    payload = result.value
    assert payload["learner_ref"] == bundle.profile.learner_ref
    assert payload["activity_summary"] == {"sessions": 1, "completed_tasks": 0}
    assert payload["mastery_summary"][0]["state"] == "DEVELOPING"
    assert payload["support_needs"] == [
        "loops：需要逐步降低 AI 辅助",
        "TASK_LOOPS：连续尝试尚未完成",
    ]
    assert payload["recent_evidence"][0]["uri"].startswith("/integrations/feishu/v1/evidence/")
    assert set(payload["redaction"]["fields_omitted"]) == {
        "credentials",
        "direct_identifiers",
        "raw_chat_text",
        "raw_source_code",
    }
    assert store.learner_reads == 1
    assert store.audits[-1]["outcome"] == "ALLOWED"
    assert store.audits[-1]["resource_id"] == bundle.profile.learner_ref
    assert store.audits[-1]["details"] == {
        "requested_fields": body["requested_fields"],
        "consent_basis": "EDUCATIONAL_SERVICE",
    }
    assert (
        _schema_errors("contracts/schemas/feishu/learner-query-result.schema.json", payload) == []
    )


def test_forged_role_cannot_bypass_actor_type_or_touch_authority() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    student_context = replace(
        _context(),
        actor=ActorRef(TENANT, "student_actor", ActorType.STUDENT, ("learner:read",)),
    )
    body = _learner_body(student_context, bundle.profile.learner_ref)

    result = asyncio.run(queries.learner_query(body, "idem_feishu_learner_0002", student_context))

    assert isinstance(result, Failure)
    assert result.error.code == "AUTHORIZATION_DENIED"
    assert store.learner_reads == 0
    assert store.audits[-1]["outcome"] == "DENIED"


def test_body_actor_and_cross_tenant_class_ref_are_rejected_before_reads() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    body = _learner_body(_context(), bundle.profile.learner_ref)
    body["context"] = {
        **body["context"],
        "actor": {**body["context"]["actor"], "actor_id": "other"},
    }

    learner_result = asyncio.run(
        queries.learner_query(body, "idem_feishu_context_0001", _context())
    )
    class_body = _class_body(_context(), stable_class_ref(SECRET, "tenant_outside_scope"))
    class_result = asyncio.run(
        queries.class_insights(class_body, "idem_feishu_class_0001", _context())
    )

    assert isinstance(learner_result, Failure)
    assert learner_result.error.code == "AUTHORIZATION_DENIED"
    assert isinstance(class_result, Failure)
    assert class_result.error.code == "AUTHORIZATION_DENIED"
    assert store.learner_reads == store.class_reads == 0
    assert [item["outcome"] for item in store.audits] == ["DENIED", "DENIED"]


def test_class_insights_release_real_fractional_distribution_without_identifiers() -> None:
    bundles = tuple(
        _bundle(
            f"learner_{index:02d}",
            failures=1 if index < 5 else 0,
            used_patch=index == 0,
        )
        for index in range(6)
    )
    store = FakeStore(bundles[0], class_bundles=bundles)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    body = _class_body(_context(), stable_class_ref(SECRET, TENANT))

    result = asyncio.run(queries.class_insights(body, "idem_feishu_class_0002", _context()))

    assert isinstance(result, Success)
    payload = result.value
    by_cell = {(item["dimension"], item["key"]): item for item in payload["insights"]}
    assert by_cell[("ENGAGEMENT", "ACTIVE")] == {
        "dimension": "ENGAGEMENT",
        "key": "ACTIVE",
        "learner_count": 6,
        "ratio": 1,
        "suppressed": False,
    }
    assert by_cell[("COMMON_ERRORS", "task_incomplete")] == {
        "dimension": "COMMON_ERRORS",
        "key": "task_incomplete",
        "learner_count": 5,
        "ratio": 5 / 6,
        "suppressed": False,
    }
    assert by_cell[("SUPPORT_NEEDS", "SKILL_PATCH_USED")]["ratio"] is None
    serialized = repr(payload)
    assert all(bundle.profile.learner_id not in serialized for bundle in bundles)
    assert payload["privacy"]["contains_learner_identifiers"] is False
    assert (
        _schema_errors("contracts/schemas/feishu/class-insights-result.schema.json", payload) == []
    )


def test_class_insights_suppress_every_cell_below_effective_cohort_minimum() -> None:
    bundles = tuple(_bundle(f"learner_small_{index:02d}") for index in range(4))
    store = FakeStore(bundles[0], class_bundles=bundles)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)

    result = asyncio.run(
        queries.class_insights(
            _class_body(_context(), stable_class_ref(SECRET, TENANT)),
            "idem_feishu_class_small_01",
            _context(),
        )
    )

    assert isinstance(result, Success)
    assert result.value["cohort_size"] == 4
    assert result.value["insights"]
    assert all(
        item["suppressed"] is True and item["learner_count"] is None and item["ratio"] is None
        for item in result.value["insights"]
    )
    assert (
        _schema_errors("contracts/schemas/feishu/class-insights-result.schema.json", result.value)
        == []
    )


def test_non_authorization_query_failures_are_audited_as_failed() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)

    missing_ref = stable_learner_ref(SECRET, TENANT, "missing_learner")
    not_found = asyncio.run(
        queries.learner_query(
            _learner_body(_context(), missing_ref),
            "idem_feishu_not_found_01",
            _context(),
        )
    )
    invalid_consent_body = _learner_body(_context(), bundle.profile.learner_ref)
    invalid_consent_body["consent_basis"] = "raw-secret-consent-value"
    invalid_consent = asyncio.run(
        queries.learner_query(
            invalid_consent_body,
            "idem_feishu_consent_0001",
            _context(),
        )
    )
    broken_profile = replace(
        bundle.profile,
        profile={**bundle.profile.profile, "updated_at": "not-a-timestamp"},
    )
    projection_store = FakeStore(replace(bundle, profile=broken_profile))
    projection_queries = FeishuLearningQueries(
        projection_store, pseudonym_secret=SECRET, clock=lambda: NOW
    )
    projection_failure = asyncio.run(
        projection_queries.learner_query(
            _learner_body(_context(), bundle.profile.learner_ref),
            "idem_feishu_projection_01",
            _context(),
        )
    )

    assert isinstance(not_found, Failure) and not_found.error.code == "NOT_FOUND"
    assert store.audits[-2]["outcome"] == "FAILED"
    assert store.audits[-2]["error_code"] == "NOT_FOUND"
    assert isinstance(invalid_consent, Failure) and invalid_consent.error.code == "INVALID_REQUEST"
    assert store.audits[-1]["outcome"] == "FAILED"
    assert store.audits[-1]["details"]["consent_basis"] == "INVALID_OR_MISSING"
    assert "raw-secret-consent-value" not in repr(store.audits[-1])
    assert isinstance(projection_failure, Failure)
    assert projection_failure.error.stage == "PROJECTION"
    assert projection_store.audits[-1]["outcome"] == "FAILED"


def test_evidence_view_uses_fixed_fact_whitelist_and_drops_raw_payload() -> None:
    learner = _bundle("learner_01", failures=2, used_patch=True)
    selected = learner.projections[-1]
    evidence_id = "evidence_source_learning_0001"
    evidence = EvidenceLearningBundle(
        evidence=EvidenceAuthority(
            evidence_id=evidence_id,
            command_id=selected.command_id,
            recorded_at=NOW - timedelta(minutes=1),
            document={
                "evidence_ref": {
                    "evidence_id": evidence_id,
                    "evidence_type": "TEST_REPORT",
                    "created_at": _timestamp(NOW - timedelta(minutes=2)),
                    "sha256": "b" * 64,
                    "uri": "/v1/raw/evidence",
                },
                "occurred_at": _timestamp(NOW - timedelta(minutes=2)),
                "payload": {
                    "evidence_kind": "SKILL_RUN",
                    "raw_source_code": "print('must never leave authority')",
                    "raw_chat_text": "private conversation",
                    "credential": "secret",
                },
                "subject": {"learner_id": learner.profile.learner_id},
            },
        ),
        profile=learner.profile,
        projection=selected,
        learner_projections=learner.projections,
    )
    store = FakeStore(learner, evidence=evidence)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)

    result = asyncio.run(queries.redacted_evidence(evidence_id, "TEACHER_SUPPORT", _context()))

    assert isinstance(result, Success)
    payload = result.value
    facts = {item["name"]: item["value"] for item in payload["facts"]}
    assert set(facts) == EVIDENCE_FACT_NAMES
    assert facts == {
        "ai_assistance_level": 4,
        "attempt_count": 2,
        "evidence_kind": "SKILL_RUN",
        "knowledge_stage": "DEVELOPING",
        "main_error": "task_incomplete",
        "run_ref": selected.run_id,
        "skill_patch_used": True,
        "task_ref": "TASK_LOOPS",
        "task_result": "NOT_COMPLETED",
    }
    assert "must never leave authority" not in repr(payload)
    assert "private conversation" not in repr(payload)
    assert "secret" not in repr(payload)
    assert learner.profile.learner_id not in repr(payload)
    assert payload["provenance"]["command_id"] == selected.command_id
    assert payload["evidence_ref"]["uri"].startswith("/integrations/feishu/v1/evidence/")
    assert store.audits[-1]["evidence_ids"] == (evidence_id,)
    assert _schema_errors("contracts/schemas/feishu/evidence-view.schema.json", payload) == []
    assert canonical_payload(payload)


def test_historical_stage_never_substitutes_the_current_profile_head() -> None:
    learner = _bundle("learner_history_without_snapshot")
    projection = replace(
        learner.projections[0],
        result={"learner": {"evidence_id": "evidence_legacy_without_snapshot"}},
    )

    facts = projection_facts_for_feishu(
        learner.profile,
        projection,
        (projection,),
        observed_now=NOW,
    )

    # The current Profile says DEMONSTRATED, but that is not historical proof.
    assert learner.profile.profile["competencies"]["loops"]["evidence_stage"] == "DEMONSTRATED"
    assert facts["knowledge_stage"] == "NOT_OBSERVED"


def test_historical_stage_uses_the_frozen_projection_snapshot_and_clock() -> None:
    learner = _bundle("learner_history_frozen_snapshot")
    projection = learner.projections[0]
    frozen_profile = {
        "competencies": {
            "loops": {
                "concept": "loops",
                "evidence_stage": "OBSERVED",
                "assistance_level": 0,
                "last_observed_at": _timestamp(projection.completed_at),
                # Due now, but it was not due when the projection was committed.
                "next_review_at": _timestamp(NOW - timedelta(minutes=1)),
                "evidence_ids": [],
            }
        }
    }
    frozen = replace(
        projection,
        result={
            **projection.result,
            "projection_receipt": {
                "receipt_json": {
                    "learner": {
                        "profile": frozen_profile,
                        "profile_sha256": canonical_json_sha256(frozen_profile),
                    }
                }
            },
        },
    )

    facts = projection_facts_for_feishu(
        learner.profile,
        frozen,
        (frozen,),
        observed_now=NOW,
    )

    assert facts["knowledge_stage"] == "EMERGING"


def test_historical_stage_rejects_a_drifted_frozen_profile_hash() -> None:
    learner = _bundle("learner_history_drifted_snapshot")
    projection = learner.projections[0]
    result = dict(projection.result)
    receipt = dict(result["projection_receipt"])
    commit = dict(receipt["receipt_json"])
    frozen_learner = dict(commit["learner"])
    frozen_learner["profile_sha256"] = "0" * 64
    commit["learner"] = frozen_learner
    receipt["receipt_json"] = commit
    result["projection_receipt"] = receipt

    with pytest.raises(ValueError, match="frozen learner profile hash drifted"):
        projection_facts_for_feishu(
            learner.profile,
            replace(projection, result=result),
            (projection,),
            observed_now=NOW,
        )


def test_opaque_references_are_stable_and_tenant_scoped() -> None:
    first = stable_learner_ref(SECRET, TENANT, "learner_01")
    assert first == stable_learner_ref(SECRET, TENANT, "learner_01")
    assert first != stable_learner_ref(SECRET, "tenant_other", "learner_01")
    assert "learner_01" not in first
    assert stable_class_ref(SECRET, TENANT) != stable_class_ref(SECRET, "tenant_other")


def test_http_learner_route_uses_locked_success_and_error_contracts() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    app = create_app(settings)
    route_context = replace(
        _context(),
        actor=ActorRef(
            TENANT,
            "feishu_teacher_01",
            ActorType.TEACHER,
            MOCK_TEACHER_ROLES,
        ),
    )
    body = _learner_body(route_context, bundle.profile.learner_ref)
    headers = _headers(route_context, "idem_feishu_http_0001")

    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        response = client.post(
            "/integrations/feishu/v1/learner-queries", headers=headers, json=body
        )
        missing_idempotency = client.post(
            "/integrations/feishu/v1/learner-queries",
            headers={key: value for key, value in headers.items() if key != "Idempotency-Key"},
            json=body,
        )

    assert response.status_code == 200
    assert response.json()["learner_ref"] == bundle.profile.learner_ref
    assert response.headers["x-trace-id"] == route_context.trace_id
    assert missing_idempotency.status_code == 400
    assert missing_idempotency.json()["error"]["code"] == "INVALID_REQUEST"
    assert [item["outcome"] for item in store.audits[-2:]] == ["ALLOWED", "FAILED"]


def test_http_request_json_and_schema_failures_are_safely_audited() -> None:
    bundle = _bundle("learner_01")
    store = FakeStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    app = create_app(settings)
    route_context = replace(
        _context(),
        actor=ActorRef(
            TENANT,
            "feishu_teacher_01",
            ActorType.TEACHER,
            MOCK_TEACHER_ROLES,
        ),
    )
    headers = _headers(route_context, "idem_feishu_invalid_body_01")
    invalid_class = _class_body(route_context, stable_class_ref(SECRET, TENANT))
    del invalid_class["purpose"]
    invalid_class["context"]["actor"] = {
        "tenant_id": "tenant_untrusted_body",
        "actor_id": "direct_identity_must_not_be_used",
        "actor_type": "teacher",
        "roles": ["class-insights:read"],
    }

    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        invalid_json = client.post(
            "/integrations/feishu/v1/learner-queries",
            headers=headers,
            content=b'{"context":',
        )
        invalid_schema = client.post(
            "/integrations/feishu/v1/class-insights",
            headers=headers,
            json=invalid_class,
        )

    assert invalid_json.status_code == 400
    assert invalid_schema.status_code == 400
    assert [item["outcome"] for item in store.audits] == ["FAILED", "FAILED"]
    assert [item["error_code"] for item in store.audits] == [
        "INVALID_REQUEST",
        "INVALID_REQUEST",
    ]
    assert [item["resource_id"] for item in store.audits] == ["invalid", "invalid"]
    assert [item["purpose"] for item in store.audits] == [None, None]
    assert [item["details"]["validation_stage"] for item in store.audits] == [
        "JSON",
        "SCHEMA",
    ]
    assert all(
        item["context"].actor.tenant_id == TENANT
        and item["details"]["request_body_identity_used"] is False
        for item in store.audits
    )
    serialized_audits = repr(store.audits)
    assert "tenant_untrusted_body" not in serialized_audits
    assert "direct_identity_must_not_be_used" not in serialized_audits
    assert store.learner_reads == 0
    assert store.class_reads == 0


def test_http_validation_audit_failure_uses_locked_error_contract() -> None:
    bundle = _bundle("learner_01")
    store = AuditFailingStore(bundle)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    app = create_app(settings)
    route_context = replace(
        _context(),
        actor=ActorRef(
            TENANT,
            "feishu_teacher_01",
            ActorType.TEACHER,
            MOCK_TEACHER_ROLES,
        ),
    )

    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        response = client.post(
            "/integrations/feishu/v1/learner-queries",
            headers=_headers(route_context, "idem_feishu_audit_failure_01"),
            content=b"{",
        )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "category": "INTERNAL",
        "retryable": False,
        "user_message_key": "system.internal_error",
        "stage": "AUDIT",
    }
    assert store.learner_reads == 0


def test_http_class_route_serializes_schema_valid_fractional_ratios() -> None:
    bundles = tuple(
        _bundle(f"learner_http_{index:02d}", failures=1 if index < 5 else 0) for index in range(6)
    )
    store = FakeStore(bundles[0], class_bundles=bundles)
    queries = FeishuLearningQueries(store, pseudonym_secret=SECRET, clock=lambda: NOW)
    settings = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    app = create_app(settings)
    route_context = replace(
        _context(),
        actor=ActorRef(
            TENANT,
            "feishu_teacher_01",
            ActorType.TEACHER,
            MOCK_TEACHER_ROLES,
        ),
    )
    body = _class_body(route_context, stable_class_ref(SECRET, TENANT))

    with TestClient(app) as client:
        app.state.feishu_learning_queries = queries
        response = client.post(
            "/integrations/feishu/v1/class-insights",
            headers=_headers(route_context, "idem_feishu_http_class_01"),
            json=body,
        )

    assert response.status_code == 200
    error_cell = next(
        item
        for item in response.json()["insights"]
        if item["dimension"] == "COMMON_ERRORS" and item["key"] == "task_incomplete"
    )
    assert error_cell == {
        "dimension": "COMMON_ERRORS",
        "key": "task_incomplete",
        "learner_count": 5,
        "ratio": 5 / 6,
        "suppressed": False,
    }
    assert (
        _schema_errors(
            "contracts/schemas/feishu/class-insights-result.schema.json", response.json()
        )
        == []
    )


def _context() -> OperationContext:
    return OperationContext(
        request_id="req_feishu_test_0001",
        correlation_id="corr_feishu_test_0001",
        trace_id="trace_feishu_test_0001",
        requested_at=NOW,
        actor=ActorRef(TENANT, "feishu_teacher_01", ActorType.TEACHER, TEACHER_ROLES),
        content_ref=ContentRef("UNIT_FEISHU", "1.0.0", "0" * 64),
        command_id="cmd_feishu_test_0001",
        causation_id=None,
    )


def _request_context(context: OperationContext) -> dict[str, Any]:
    return {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": _timestamp(context.requested_at),
        "actor": {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        },
        "content_ref": {
            "unit_id": "UNIT_FEISHU",
            "version": "1.0.0",
            "content_hash": CONTENT_HASH,
        },
    }


def _headers(context: OperationContext, idempotency_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TENANT}:feishu_teacher_01",
        "X-Request-Id": context.request_id,
        "X-Trace-Id": context.trace_id,
        "X-Correlation-Id": context.correlation_id,
        "X-Schema-Version": context.schema_version,
        "Idempotency-Key": idempotency_key,
    }


def _learner_body(context: OperationContext, learner_ref: str) -> dict[str, Any]:
    return {
        "context": _request_context(context),
        "learner_ref": learner_ref,
        "purpose": "TEACHER_SUPPORT",
        "requested_fields": [
            "MASTERY_SUMMARY",
            "RECENT_EVIDENCE",
            "SUPPORT_NEEDS",
            "ACTIVITY_SUMMARY",
            "RECOMMENDED_NEXT_STEPS",
            "DATA_FRESHNESS",
        ],
        "consent_basis": "EDUCATIONAL_SERVICE",
    }


def _class_body(context: OperationContext, class_ref: str) -> dict[str, Any]:
    return {
        "context": _request_context(context),
        "class_ref": class_ref,
        "purpose": "TEACHER_PLANNING",
        "time_range": {
            "from": _timestamp(NOW - timedelta(days=7)),
            "to": _timestamp(NOW),
        },
        "dimensions": [
            "CONCEPT_MASTERY",
            "COMMON_ERRORS",
            "SUPPORT_NEEDS",
            "ENGAGEMENT",
            "COMPLETION",
        ],
        "privacy": {"minimum_cohort_size": 5, "suppress_small_cells": True},
    }


def _bundle(
    learner_id: str,
    *,
    failures: int = 1,
    used_patch: bool = False,
) -> LearnerLearningBundle:
    suffix = hashlib.sha256(learner_id.encode()).hexdigest()[:16]
    evidence_id = f"evidence_source_{suffix}"
    learner_ref = stable_learner_ref(SECRET, TENANT, learner_id)
    profile = LearnerProfileAuthority(
        learner_ref=learner_ref,
        tenant_id=TENANT,
        learner_id=learner_id,
        actor_id=f"student_actor_{learner_id}",
        content_hash=CONTENT_HASH,
        profile={
            "schema_version": "1.0.0",
            "learner_id": learner_id,
            "actor_id": f"student_actor_{learner_id}",
            "content": {
                "unit_id": "UNIT_FEISHU",
                "version": "1.0.0",
                "content_hash": CONTENT_HASH,
            },
            "competencies": {
                "loops": {
                    "concept": "loops",
                    "evidence_stage": "DEMONSTRATED",
                    "assistance_level": 4 if used_patch else 0,
                    "last_observed_at": _timestamp(NOW - timedelta(minutes=1)),
                    "next_review_at": _timestamp(NOW + timedelta(days=1)),
                    "evidence_ids": [evidence_id],
                }
            },
            "evidence_refs": [
                {
                    "evidence_id": evidence_id,
                    "evidence_type": "TEST_REPORT",
                    "created_at": _timestamp(NOW - timedelta(minutes=2)),
                    "sha256": "b" * 64,
                    "uri": "/v1/raw/evidence",
                }
            ],
            "updated_at": _timestamp(NOW - timedelta(minutes=1)),
        },
        updated_at=NOW - timedelta(minutes=1),
    )
    projections = tuple(
        _projection(learner_id, sequence, failed=sequence <= failures, used_patch=used_patch)
        for sequence in range(1, max(1, failures) + 1)
    )
    return LearnerLearningBundle(profile=profile, projections=projections)


def _projection(
    learner_id: str, sequence: int, *, failed: bool, used_patch: bool
) -> LearningProjectionAuthority:
    suffix = hashlib.sha256(learner_id.encode()).hexdigest()[:16]
    frozen_profile = {
        "competencies": {
            "loops": {
                "concept": "loops",
                "evidence_stage": "DEMONSTRATED",
                "assistance_level": 4 if used_patch else 0,
                "last_observed_at": _timestamp(NOW - timedelta(minutes=1)),
                "next_review_at": _timestamp(NOW + timedelta(days=1)),
                "evidence_ids": [f"evidence_source_{suffix}"],
            }
        }
    }
    return LearningProjectionAuthority(
        job_id=f"job_{suffix}_{sequence}",
        command_id=f"cmd_{suffix}_{sequence:04d}",
        session_id=f"session_{suffix}",
        turn_id=f"turn_{suffix}_{sequence}",
        run_id=f"run_{suffix}_{sequence}",
        learner_id=learner_id,
        source_event_id=f"event_{suffix}_{sequence}",
        through_sequence=sequence,
        projection={
            "run": {
                "run_id": f"run_{suffix}_{sequence}",
                "task_success": not failed,
                "failure_key": "task_incomplete" if failed else None,
            },
            "task": {"task_id": "TASK_LOOPS", "concept": "loops"},
            "assistance": {
                "assistance_authority": "SKILL_PATCH" if used_patch else "NONE",
                "used_skill_patch": used_patch,
            },
            "source_feedback_event_id": f"event_feedback_{suffix}_{sequence}",
            "source_evidence_ids": [f"evidence_source_{suffix}"],
        },
        result={
            "learner": {"evidence_id": f"evidence_learner_{suffix}_{sequence}"},
            "projection_receipt": {
                "receipt_json": {
                    "learner": {
                        "profile": frozen_profile,
                        "profile_sha256": canonical_json_sha256(frozen_profile),
                    }
                }
            },
        },
        completed_at=NOW - timedelta(minutes=max(0, 5 - sequence)),
    )


def _not_found():
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        "NOT_FOUND",
        ErrorCategory.VALIDATION,
        False,
        "resource.not_found",
        "READ",
        "not found",
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _schema_errors(path: str, payload: object) -> list[str]:
    release = ContractRelease(Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH))
    return release.validate(path, payload)
