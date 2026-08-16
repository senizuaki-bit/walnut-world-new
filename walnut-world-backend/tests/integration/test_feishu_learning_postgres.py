"""Real PostgreSQL closure for the read-only Feishu learning adapter."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    Failure,
    OperationContext,
    Success,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres.feishu_learning import PostgresFeishuLearningStore
from walnut_backend.adapters.postgres.models import (
    AuditRow,
    EvidenceRow,
    LearnerProfileRow,
    LearnerProjectionJobRow,
    ProductContentUnitRow,
    ProductDraftRow,
    RunRow,
    SkillActivationRow,
    SkillBuildRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.api.app import create_app
from walnut_backend.application.feishu.learning_queries import stable_learner_ref
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

SECRET = "feishu-postgres-integration-secret-" + "p" * 40
CONTENT_HASH = "a" * 64
BUSINESS_MODELS = (
    ProductContentUnitRow,
    WorldSnapshotRow,
    LearnerProfileRow,
    ProductDraftRow,
    SkillBuildRow,
    SkillActivationRow,
    RunRow,
    EvidenceRow,
    LearnerProjectionJobRow,
)


def test_feishu_store_filters_tenant_persists_audit_and_writes_no_business_rows() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required Feishu PostgreSQL coverage"
        )
    asyncio.run(_exercise_store(database_url))


def test_feishu_mcp_resolves_minimal_inputs_and_disambiguates_against_postgres() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required Feishu MCP PostgreSQL coverage"
        )
    suffix = uuid4().hex
    tenant_a = f"tenant_feishu_mcp_a_{suffix}"
    tenant_b = f"tenant_feishu_mcp_b_{suffix}"
    learner_id = f"learner_feishu_mcp_{suffix}"
    learner_ref = stable_learner_ref(SECRET, tenant_a, learner_id)
    before = asyncio.run(_business_counts_for_url(database_url))
    asyncio.run(
        _insert_profiles(
            database_url,
            (
                _profile_row(tenant_a, learner_id, f"student_a_{suffix}", "UNIT_MCP_PG", "a" * 64),
                _profile_row(tenant_b, learner_id, f"student_b_{suffix}", "UNIT_MCP_PG", "a" * 64),
            ),
        )
    )
    try:
        settings = replace(
            Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH),
            database_url=database_url,
            feishu_pseudonym_secret=SECRET,
        )
        with TestClient(create_app(settings)) as client:
            learner = _mcp_call(
                client,
                tenant_a,
                f"feishu_teacher_{suffix}",
                "query_learner_progress",
                {"learner_ref": learner_ref},
                "learner-minimal",
            )
            class_minimal = _mcp_call(
                client,
                tenant_a,
                f"feishu_teacher_{suffix}",
                "query_class_common_issues",
                {},
                "class-minimal",
            )
            cross_tenant = _mcp_call(
                client,
                tenant_b,
                f"feishu_teacher_{suffix}",
                "query_learner_progress",
                {"learner_ref": learner_ref},
                "learner-cross-tenant",
            )
            student = _mcp_call(
                client,
                tenant_a,
                f"student_{suffix}",
                "query_learner_progress",
                {"learner_ref": learner_ref},
                "learner-student",
            )

        assert learner["isError"] is False
        assert learner["structuredContent"]["learner"]["learner_ref"] == learner_ref
        assert class_minimal["isError"] is False
        assert class_minimal["structuredContent"]["class_insights"]["cohort_size"] == 1
        assert _mcp_error_code(cross_tenant) == "NOT_FOUND"
        assert _mcp_error_code(student) == "AUTHORIZATION_DENIED"

        asyncio.run(
            _insert_profiles(
                database_url,
                (
                    _profile_row(
                        tenant_a,
                        f"learner_second_{suffix}",
                        f"student_second_{suffix}",
                        "UNIT_MCP_PG_OTHER",
                        "b" * 64,
                        version="2.0.0",
                    ),
                ),
            )
        )
        with TestClient(create_app(settings)) as client:
            ambiguous = _mcp_call(
                client,
                tenant_a,
                f"feishu_teacher_{suffix}",
                "query_class_common_issues",
                {},
                "class-ambiguous",
            )
            explicit = _mcp_call(
                client,
                tenant_a,
                f"feishu_teacher_{suffix}",
                "query_class_common_issues",
                {
                    "content_ref": {
                        "unit_id": "UNIT_MCP_PG",
                        "version": "1.0.0",
                        "content_hash": "a" * 64,
                    }
                },
                "class-explicit",
            )

        assert _mcp_error_code(ambiguous) == "INVALID_REQUEST"
        assert explicit["isError"] is False
        assert explicit["structuredContent"]["class_insights"]["cohort_size"] == 1
        audits = asyncio.run(_mcp_audits(database_url, (tenant_a, tenant_b)))
        assert any(
            row.operation == "FEISHU_MCP_LEARNER_CONTENT_RESOLVE"
            and row.outcome == "DENIED"
            and row.record_json["actor"]["actor_type"] == "student"
            for row in audits
        )
        assert any(
            row.operation == "FEISHU_MCP_CLASS_CONTENT_RESOLVE"
            and row.outcome == "FAILED"
            and row.record_json["error_code"] == "INVALID_REQUEST"
            for row in audits
        )
    finally:
        asyncio.run(_cleanup_mcp_test(database_url, (tenant_a, tenant_b)))
    assert asyncio.run(_business_counts_for_url(database_url)) == before


async def _exercise_store(database_url: str) -> None:
    suffix = uuid4().hex
    tenant_a = f"tenant_feishu_pg_a_{suffix}"
    tenant_b = f"tenant_feishu_pg_b_{suffix}"
    learner_id = f"learner_feishu_pg_{suffix}"
    actor_id = f"student_feishu_pg_{suffix}"
    operation = f"FEISHU_PG_INTEGRATION_{suffix}"
    now = datetime.now(UTC)
    sessions = create_session_factory(database_url)
    store = PostgresFeishuLearningStore(sessions, pseudonym_secret=SECRET)
    profile_json = {
        "learner_id": learner_id,
        "actor_id": actor_id,
        "content": {
            "unit_id": "UNIT_FEISHU_PG",
            "version": "1.0.0",
            "content_hash": CONTENT_HASH,
        },
        "competencies": {},
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    try:
        async with sessions() as session, session.begin():
            session.add_all(
                LearnerProfileRow(
                    tenant_id=tenant_id,
                    learner_id=learner_id,
                    actor_id=actor_id,
                    content_hash=CONTENT_HASH,
                    profile_sha256=canonical_json_sha256(profile_json),
                    profile_json=profile_json,
                    created_at=now,
                    updated_at=now,
                )
                for tenant_id in (tenant_a, tenant_b)
            )

        before = await _business_counts(sessions)
        learner_ref_a = stable_learner_ref(SECRET, tenant_a, learner_id)
        own = await store.learner_bundle(tenant_a, learner_ref_a, None, None)
        cross_tenant = await store.learner_bundle(tenant_b, learner_ref_a, None, None)

        assert isinstance(own, Success)
        assert own.value.profile.tenant_id == tenant_a
        assert own.value.profile.learner_id == learner_id
        assert own.value.projections == ()
        assert isinstance(cross_tenant, Failure)
        assert cross_tenant.error.code == "NOT_FOUND"

        context = OperationContext(
            request_id=f"req_{suffix}",
            correlation_id=f"corr_{suffix}",
            trace_id=f"trace_{suffix}",
            requested_at=now,
            actor=ActorRef(
                tenant_a,
                f"teacher_{suffix}",
                ActorType.TEACHER,
                ("learner:read", "teacher"),
            ),
            content_ref=ContentRef("UNIT_FEISHU_PG", "1.0.0", CONTENT_HASH),
            command_id=f"cmd_{suffix}",
            causation_id=None,
        )
        appended = await store.append_access_audit(
            context=context,
            operation=operation,
            outcome="ALLOWED",
            resource_type="LEARNER_PROFILE",
            resource_id=learner_ref_a,
            purpose="TEACHER_SUPPORT",
            evidence_ids=(),
            error_code=None,
            details={"adapter_integration": True},
        )
        assert isinstance(appended, Success)

        async with sessions() as session:
            audit = await session.scalar(
                select(AuditRow).where(
                    AuditRow.tenant_id == tenant_a,
                    AuditRow.operation == operation,
                )
            )
        assert audit is not None
        assert audit.outcome == "ALLOWED"
        assert audit.record_json["resource_id"] == learner_ref_a
        assert audit.record_json["details"] == {"adapter_integration": True}
        assert await _business_counts(sessions) == before
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                delete(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id.in_((tenant_a, tenant_b))
                )
            )
        await sessions.kw["bind"].dispose()


async def _business_counts(session_factory: object) -> tuple[int, ...]:
    async with session_factory() as session:  # type: ignore[operator]
        counts: list[int] = []
        for model in BUSINESS_MODELS:
            value = await session.scalar(select(func.count()).select_from(model))
            counts.append(int(value or 0))
        return tuple(counts)


async def _business_counts_for_url(database_url: str) -> tuple[int, ...]:
    sessions = create_session_factory(database_url)
    try:
        return await _business_counts(sessions)
    finally:
        await sessions.kw["bind"].dispose()


def _profile_row(
    tenant_id: str,
    learner_id: str,
    actor_id: str,
    unit_id: str,
    content_hash: str,
    *,
    version: str = "1.0.0",
) -> LearnerProfileRow:
    now = datetime.now(UTC)
    profile = {
        "schema_version": "1.0.0",
        "learner_id": learner_id,
        "actor_id": actor_id,
        "content": {
            "unit_id": unit_id,
            "version": version,
            "content_hash": content_hash,
        },
        "competencies": {},
        "evidence_refs": [],
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    return LearnerProfileRow(
        tenant_id=tenant_id,
        learner_id=learner_id,
        actor_id=actor_id,
        content_hash=content_hash,
        profile_sha256=canonical_json_sha256(profile),
        profile_json=profile,
        created_at=now,
        updated_at=now,
    )


async def _insert_profiles(
    database_url: str, profiles: tuple[LearnerProfileRow, ...]
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            session.add_all(profiles)
    finally:
        await sessions.kw["bind"].dispose()


def _mcp_call(
    client: TestClient,
    tenant_id: str,
    actor_id: str,
    tool: str,
    arguments: dict[str, object],
    request_id: str,
) -> dict[str, object]:
    response = client.post(
        "/integrations/feishu/v1/mcp",
        headers={
            "Authorization": f"Bearer {tenant_id}:{actor_id}",
            "MCP-Protocol-Version": "2025-06-18",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def _mcp_error_code(result: dict[str, object]) -> str:
    assert result["isError"] is True
    content = result["content"]
    assert isinstance(content, list)
    text = content[0]["text"]
    return json.loads(text)["error"]["code"]


async def _mcp_audits(
    database_url: str, tenant_ids: tuple[str, ...]
) -> list[AuditRow]:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(AuditRow)
                        .where(AuditRow.tenant_id.in_(tenant_ids))
                        .order_by(AuditRow.occurred_at, AuditRow.audit_id)
                    )
                ).all()
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _cleanup_mcp_test(database_url: str, tenant_ids: tuple[str, ...]) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                delete(LearnerProfileRow).where(LearnerProfileRow.tenant_id.in_(tenant_ids))
            )
    finally:
        await sessions.kw["bind"].dispose()
