"""Real PostgreSQL upgrade gates for the INT2 immutable authority cutover."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from yaya_agent_build import canonical_source_bundle_sha256
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    OperationContext,
    Success,
    canonical_json_sha256,
)

from tests.integration._session_authority_support import seed_session_launch_authority
from walnut_backend.adapters.postgres.certification_authority import (
    validate_certification_authority,
)
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import SkillCertificationRow
from walnut_backend.adapters.postgres.product_drafts import draft_resource
from walnut_backend.adapters.postgres.session import (
    create_session_factory,
    normalize_database_url,
)
from walnut_backend.adapters.postgres.skill_builds import PostgresSkillBuildStore
from walnut_backend.adapters.postgres.workflow_jobs import (
    workflow_step_receipt_id,
)
from walnut_backend.certified_skill_schema import certified_parameter_schema

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_018 = "018_world_presentation_events"
REVISION_019 = "019_int2_skill_patch_authority"


@pytest.fixture
def scratch_database_url() -> Iterator[str]:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required INT2 migration coverage"
        )
    base = make_url(normalize_database_url(database_url))
    database_name = f"walnut_int2_{uuid.uuid4().hex[:20]}"
    target = base.set(database=database_name)
    asyncio.run(_create_database(base.set(database="postgres"), database_name))
    try:
        yield target.render_as_string(hide_password=False)
    finally:
        asyncio.run(_drop_database(base.set(database="postgres"), database_name))


def test_int2_valid_legacy_draft_upgrade_downgrade_roundtrip(
    scratch_database_url: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    original = asyncio.run(_seed_legacy_draft(scratch_database_url))

    _migrate(scratch_database_url, REVISION_019)
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_019
    assert state["draft_revision_count"] == 1
    assert state["draft_json"] == original

    _migrate(scratch_database_url, REVISION_018, downgrade=True)
    downgraded = asyncio.run(_migration_state(scratch_database_url))
    assert downgraded == {
        "revision": REVISION_018,
        "draft_revision_count": None,
        "draft_revision_created_at": None,
        "draft_json": original,
    }

    _migrate(scratch_database_url, REVISION_019)
    repeated = asyncio.run(_migration_state(scratch_database_url))
    assert repeated["revision"] == REVISION_019
    assert repeated["draft_revision_count"] == 1
    assert repeated["draft_json"] == original


def test_int2_legacy_mutated_head_uses_revision_update_time(
    scratch_database_url: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    original = asyncio.run(
        _seed_legacy_draft(
            scratch_database_url,
            revision=2,
            updated_at=datetime(2026, 8, 14, 2, 3, 4, 567890, tzinfo=UTC),
        )
    )

    _migrate(scratch_database_url, REVISION_019)
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["draft_revision_created_at"].isoformat().replace(
        "+00:00", "Z"
    ) == original["updated_at"]


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("timestamp_mirror", id="timestamp-mirror"),
        pytest.param("session_content", id="session-content"),
        pytest.param("source_content_hash", id="source-content-hash"),
        pytest.param("source_case_collision", id="source-case-collision"),
        pytest.param("unsafe_integer", id="unsafe-integer"),
    ],
)
def test_int2_corrupt_legacy_draft_rolls_back_entire_upgrade(
    scratch_database_url: str,
    corrupt: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(_seed_legacy_draft(scratch_database_url))
    asyncio.run(_corrupt_legacy_draft(scratch_database_url, corrupt))

    completed = _run_alembic(scratch_database_url, "upgrade", REVISION_019)
    assert completed.returncode != 0
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_018
    assert state["draft_revision_count"] is None


@pytest.mark.parametrize(
    ("corrupt", "expected_error"),
    [
        pytest.param(
            "orphan_run",
            "legacy Learner projection authority closure is incomplete",
            id="orphan-run",
        ),
        pytest.param(
            "request_sha256",
            "legacy Learner objective request bytes drifted",
            id="request-sha256",
        ),
        pytest.param(
            "result_sha256",
            "legacy Learner terminal result bytes drifted",
            id="result-sha256",
        ),
        pytest.param(
            "receipt_identity",
            "legacy Learner terminal receipt bytes drifted",
            id="receipt-identity",
        ),
    ],
)
def test_int2_invalid_legacy_learner_projection_rolls_back_entire_upgrade(
    scratch_database_url: str,
    corrupt: str,
    expected_error: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(
        _seed_orphan_legacy_learner_projection(
            scratch_database_url,
            corrupt=corrupt,
        )
    )

    completed = _run_alembic(scratch_database_url, "upgrade", REVISION_019)
    assert completed.returncode != 0
    assert expected_error in completed.stderr
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_018
    assert state["draft_revision_count"] is None


@pytest.mark.parametrize(
    ("corrupt", "expected_error"),
    [
        pytest.param(
            "request_sha256",
            "INT2 Learner objective request bytes drifted",
            id="request-sha256",
        ),
        pytest.param(
            "result_sha256",
            "INT2 Learner terminal result bytes drifted",
            id="result-sha256",
        ),
        pytest.param(
            "receipt_identity",
            "INT2 Learner terminal receipt bytes drifted",
            id="receipt-identity",
        ),
    ],
)
def test_int2_invalid_learner_projection_rolls_back_entire_downgrade(
    scratch_database_url: str,
    corrupt: str,
    expected_error: str,
) -> None:
    _migrate(scratch_database_url, REVISION_019)
    asyncio.run(
        _seed_orphan_legacy_learner_projection(
            scratch_database_url,
            corrupt=corrupt,
            include_assistance=True,
        )
    )

    completed = _run_alembic(scratch_database_url, "downgrade", REVISION_018)
    assert completed.returncode != 0
    assert expected_error in completed.stderr
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_019
    assert state["draft_revision_count"] == 0


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("build_timestamp", id="build-timestamp"),
        pytest.param("command_status", id="command-status"),
        pytest.param("job_extra_key", id="job-extra-key"),
        pytest.param("job_fence", id="job-fence"),
        pytest.param("idempotency_request", id="idempotency-request"),
        pytest.param("receipt_identity", id="receipt-identity"),
        pytest.param("receipt_body", id="receipt-body"),
        pytest.param("stray_evidence", id="stray-evidence"),
        pytest.param("launch_inactive", id="launch-inactive"),
        pytest.param("policy_inactive", id="policy-inactive"),
        pytest.param("policy_hash", id="policy-hash"),
        pytest.param("policy_schema", id="policy-schema"),
        pytest.param("request_context_extra", id="request-context-extra"),
        pytest.param("version_hash", id="version-hash"),
    ],
)
def test_int2_invalid_legacy_build_authority_rolls_back_entire_upgrade(
    scratch_database_url: str,
    corrupt: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(
        _seed_rejected_legacy_build(
            scratch_database_url,
            corrupt=corrupt,
        )
    )

    completed = _run_alembic(scratch_database_url, "upgrade", REVISION_019)
    assert completed.returncode != 0
    if corrupt in {"job_fence", "receipt_identity", "receipt_body"}:
        expected_error = "legacy Build terminal receipt authority is corrupt"
    elif corrupt in {
        "stray_evidence",
        "launch_inactive",
        "policy_inactive",
        "policy_hash",
        "policy_schema",
    }:
        expected_error = "legacy Build Launch/Policy/Evidence authority is corrupt"
    else:
        expected_error = "legacy Build command/request authority is corrupt"
    assert expected_error in completed.stderr
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_018
    assert state["draft_revision_count"] is None


def test_int2_valid_rejected_legacy_build_upgrade_downgrade_roundtrip(
    scratch_database_url: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(
        _seed_rejected_legacy_build(
            scratch_database_url,
            corrupt="none",
        )
    )
    original = asyncio.run(_legacy_build_state(scratch_database_url))

    _migrate(scratch_database_url, REVISION_019)
    upgraded = asyncio.run(_legacy_build_state(scratch_database_url))
    assert upgraded["revision"] == REVISION_019
    assert upgraded["provenance_count"] == 1
    assert upgraded["marker_count"] == 1
    assert upgraded["job"]["job_json"] == {
        **original["job"]["job_json"],
        "build_provenance_sha256": upgraded["provenance_sha256"],
    }
    assert asyncio.run(_read_legacy_build(scratch_database_url))["status"] == "REJECTED"

    _migrate(scratch_database_url, REVISION_018, downgrade=True)
    downgraded = asyncio.run(_legacy_build_state(scratch_database_url))
    assert downgraded == original

    _migrate(scratch_database_url, REVISION_019)
    repeated = asyncio.run(_legacy_build_state(scratch_database_url))
    assert repeated == upgraded
    assert asyncio.run(_read_legacy_build(scratch_database_url))["status"] == "REJECTED"


def test_int2_valid_accepted_legacy_build_upgrade_downgrade_roundtrip(
    scratch_database_url: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(_seed_accepted_legacy_build(scratch_database_url, corrupt="none"))
    original = asyncio.run(_legacy_build_state(scratch_database_url))

    _migrate(scratch_database_url, REVISION_019)
    upgraded = asyncio.run(_legacy_build_state(scratch_database_url))
    assert upgraded["revision"] == REVISION_019
    assert upgraded["provenance_count"] == 1
    assert upgraded["marker_count"] == 1
    assert upgraded["terminal_count"] == 0
    assert asyncio.run(_read_legacy_build(scratch_database_url))["status"] == "ACCEPTED"

    _migrate(scratch_database_url, REVISION_018, downgrade=True)
    assert asyncio.run(_legacy_build_state(scratch_database_url)) == original

    _migrate(scratch_database_url, REVISION_019)
    assert asyncio.run(_legacy_build_state(scratch_database_url)) == upgraded
    assert asyncio.run(_read_legacy_build(scratch_database_url))["status"] == "ACCEPTED"


def test_int2_valid_certified_legacy_build_upgrades_and_reads(
    scratch_database_url: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(_seed_certified_legacy_build(scratch_database_url))
    original = asyncio.run(_legacy_build_state(scratch_database_url))

    _migrate(scratch_database_url, REVISION_019)
    upgraded = asyncio.run(_legacy_build_state(scratch_database_url))

    assert upgraded["provenance_count"] == 1
    assert upgraded["terminal_count"] == 1
    assert upgraded["certification_provenance_count"] == 1
    assert asyncio.run(_read_legacy_build(scratch_database_url))["status"] == "CERTIFIED"
    asyncio.run(_assert_legacy_certification_valid(scratch_database_url))

    _migrate(scratch_database_url, REVISION_018, downgrade=True)
    assert asyncio.run(_legacy_build_state(scratch_database_url)) == original

    _migrate(scratch_database_url, REVISION_019)
    assert asyncio.run(_legacy_build_state(scratch_database_url)) == upgraded
    assert asyncio.run(_read_legacy_build(scratch_database_url))["status"] == "CERTIFIED"
    asyncio.run(_assert_legacy_certification_valid(scratch_database_url))


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("missing_evidence", id="missing-evidence"),
        pytest.param("duplicate_evidence", id="duplicate-evidence"),
        pytest.param("wrong_evidence_command", id="wrong-evidence-command"),
        pytest.param("evidence_payload", id="evidence-payload"),
        pytest.param("artifact_metadata", id="artifact-metadata"),
        pytest.param("cert_schema", id="cert-schema"),
        pytest.param("terminal_receipt_identity", id="terminal-receipt-identity"),
        pytest.param("terminal_job_phase", id="terminal-job-phase"),
        pytest.param("build_artifact_projection", id="build-artifact-projection"),
        pytest.param("empty_capability", id="empty-capability"),
        pytest.param("empty_certification_tuple", id="empty-certification-tuple"),
    ],
)
def test_int2_invalid_certified_legacy_build_rolls_back_upgrade(
    scratch_database_url: str,
    corrupt: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(_seed_certified_legacy_build(scratch_database_url, corrupt=corrupt))

    completed = _run_alembic(scratch_database_url, "upgrade", REVISION_019)

    assert completed.returncode != 0
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_018
    assert state["draft_revision_count"] is None


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("evidence_payload", id="evidence-payload"),
        pytest.param("artifact_metadata", id="artifact-metadata"),
        pytest.param("cert_schema", id="cert-schema"),
    ],
)
def test_int2_certified_authority_drift_blocks_downgrade(
    scratch_database_url: str,
    corrupt: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(_seed_certified_legacy_build(scratch_database_url))
    _migrate(scratch_database_url, REVISION_019)
    asyncio.run(_corrupt_certified_authority(scratch_database_url, corrupt=corrupt))

    completed = _run_alembic(scratch_database_url, "downgrade", REVISION_018)

    assert completed.returncode != 0
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_019
    assert state["draft_revision_count"] == 0


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("terminal_command", id="terminal-command"),
        pytest.param("terminal_job", id="terminal-job"),
    ],
)
def test_int2_invalid_accepted_legacy_build_state_rolls_back_upgrade(
    scratch_database_url: str,
    corrupt: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(_seed_accepted_legacy_build(scratch_database_url, corrupt=corrupt))

    completed = _run_alembic(scratch_database_url, "upgrade", REVISION_019)

    assert completed.returncode != 0
    assert "legacy Build nonterminal authority is corrupt" in completed.stderr
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_018
    assert state["draft_revision_count"] is None


def test_int2_rejected_build_read_rejects_coordinated_request_authority_drift(
    scratch_database_url: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(
        _seed_rejected_legacy_build(
            scratch_database_url,
            corrupt="none",
        )
    )
    _migrate(scratch_database_url, REVISION_019)
    assert asyncio.run(_read_legacy_build(scratch_database_url))["status"] == "REJECTED"

    asyncio.run(_rewrite_rejected_build_request_authority(scratch_database_url))

    with pytest.raises(AssertionError, match="Build durable authority drifted"):
        asyncio.run(_read_legacy_build(scratch_database_url))


def test_int2_rejected_build_authority_drift_blocks_downgrade(
    scratch_database_url: str,
) -> None:
    _migrate(scratch_database_url, REVISION_018)
    asyncio.run(
        _seed_rejected_legacy_build(
            scratch_database_url,
            corrupt="none",
        )
    )
    _migrate(scratch_database_url, REVISION_019)
    asyncio.run(_rewrite_rejected_build_request_authority(scratch_database_url))

    completed = _run_alembic(scratch_database_url, "downgrade", REVISION_018)

    assert completed.returncode != 0
    assert "INT2 Build acceptance authority drifted before downgrade" in completed.stderr
    state = asyncio.run(_migration_state(scratch_database_url))
    assert state["revision"] == REVISION_019
    assert state["draft_revision_count"] == 0


async def _seed_rejected_legacy_build(
    database_url: str,
    *,
    corrupt: str,
) -> None:
    accepted_at = datetime(2026, 8, 14, 4, 5, 6, 789012, tzinfo=UTC)
    completed_at = accepted_at + timedelta(seconds=5)
    accepted_wire = accepted_at.isoformat().replace("+00:00", "Z")
    completed_wire = completed_at.isoformat().replace("+00:00", "Z")
    accepted_command_wire = accepted_at.isoformat()
    completed_command_wire = completed_at.isoformat()
    tenant_id = "tenant_int2_legacy_build"
    actor_id = "learner_int2_legacy_build"
    command_id = "cmd_int2_legacy_build"
    build_id = "build_" + hashlib.sha256(command_id.encode()).hexdigest()[:24]
    job_id = _scoped_id("job", tenant_id, command_id)
    request_sha256 = "2" * 64
    source_text = "int main() { return 0; }\n"
    content_sha256 = hashlib.sha256(source_text.encode()).hexdigest()
    source_bundle = {
        "language": "CPP20",
        "entrypoint": "src/main.cpp",
        "files": [
            {
                "path": "src/main.cpp",
                "content": source_text,
                "content_sha256": content_sha256,
            }
        ],
    }
    source_sha256 = hashlib.sha256(
        _json([["src/main.cpp", content_sha256]]).encode()
    ).hexdigest()
    content_ref = {
        "unit_id": "CONTENT_INT2_LEGACY_BUILD",
        "version": "1.0.0",
        "content_hash": "1" * 64,
    }
    versions = await seed_session_launch_authority(
        database_url,
        tenant_id=tenant_id,
        actor_id=actor_id,
        request={
            "content": content_ref,
            "world_id": "world_int2_legacy_build",
            "learner_id": actor_id,
            "agent_profile_id": "agent_profile_int2_legacy_build",
            "channel": "GAME",
            "locale": "zh-CN",
        },
    )
    if corrupt == "version_hash":
        versions["artifact_sha256"] = "x"
    request_context = {
        "request_id": "req_int2_legacy_build",
        "correlation_id": "corr_int2_legacy_build",
        "trace_id": "trace_int2_legacy_build",
        "requested_at": accepted_command_wire,
        "actor": {
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "actor_type": "student",
            "roles": ["game:player"],
        },
        "content_ref": content_ref,
        "schema_version": "1.0.0",
    }
    if corrupt == "request_context_extra":
        request_context["unexpected"] = "drift"
    request = {
        "skill_id": "skill_int2_legacy_build",
        "display_name": "Legacy rejected Build",
        "client_draft_revision": 1,
        "source_bundle": source_bundle,
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "test_suite_version": "test-suite-1",
        "requested_capabilities": ["WORLD_READ"],
    }
    details = {
        "diagnostic_codes": ["INVALID_SOURCE"],
        "pipeline_code": "INVALID_SOURCE",
    }
    build_error = {
        "code": "INVALID_REQUEST",
        "category": "VALIDATION",
        "retryable": False,
        "user_message_key": "request.invalid",
        "stage": "VALIDATE_SOURCE",
        "message": "Skill Build did not satisfy the server certification policy.",
        "details": details,
        "evidence_ids": [],
    }
    command_error = {**build_error, "stage": "VALIDATE"}
    phases = [
        {
            "name": "VALIDATE_SOURCE",
            "status": "FAILED",
            "started_at": accepted_wire,
            "finished_at": completed_wire,
            "diagnostic_codes": ["INVALID_SOURCE"],
        },
        *[
            {
                "name": name,
                "status": "SKIPPED",
                "started_at": None,
                "finished_at": None,
                "diagnostic_codes": [],
            }
            for name in ("COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY")
        ],
    ]
    build = {
        "request_context": request_context,
        "build_id": build_id,
        "skill_id": request["skill_id"],
        "skill_version_id": None,
        "status": "REJECTED",
        "terminal": True,
        "created_at": (
            "2050-01-01T00:00:00Z"
            if corrupt == "build_timestamp"
            else accepted_wire
        ),
        "updated_at": completed_wire,
        "artifact": None,
        "certification": None,
        "phases": phases,
        "failure": build_error,
        "evidence_refs": [],
        "versions": versions,
    }
    command = {
        "request_context": request_context,
        "command_id": command_id,
        "command_type": "CREATE_SKILL_BUILD",
        "status": "REJECTED",
        "stage": "VALIDATE",
        "terminal": True,
        "accepted_at": accepted_command_wire,
        "updated_at": completed_command_wire,
        "result": None,
        "error": command_error,
        "evidence_refs": [],
        "versions": versions,
        "links": {"self": f"/v1/commands/{command_id}"},
        "revision": 3,
    }
    if corrupt == "command_status":
        command["status"] = "FAILED"
    job = {
        "schema_version": "1.0.0",
        "request_context": request_context,
        "build_id": build_id,
        "request": request,
    }
    if corrupt == "job_extra_key":
        job["unexpected"] = "drift"
    receipt_output = {
        "build_id": build_id,
        "failure_code": "INVALID_SOURCE",
        "failure_stage": "VALIDATE_SOURCE",
        "diagnostic_codes": ["INVALID_SOURCE"],
        "source_sha256": source_sha256,
        "build_identity": "legacy-build-identity-v1",
    }
    if corrupt == "receipt_body":
        receipt_output["build_id"] = "build_int2_wrong_authority"
    receipt_id = workflow_step_receipt_id(
        tenant_id,
        job_id,
        "BUILD_REJECTED",
    )
    if corrupt == "receipt_identity":
        receipt_id = "receipt_int2_wrong_build_identity"
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO commands "
                    "(command_id,tenant_id,actor_id,command_type,status,revision,terminal,"
                    "accepted_at,updated_at,record_json) VALUES "
                    "(:command_id,:tenant_id,:actor_id,'CREATE_SKILL_BUILD','REJECTED',3,true,"
                    ":accepted_at,:updated_at,CAST(:record_json AS jsonb))"
                ),
                {
                    "command_id": command_id,
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "accepted_at": accepted_at,
                    "updated_at": completed_at,
                    "record_json": _json(command),
                },
            )
            if corrupt == "stray_evidence":
                await connection.execute(
                    text(
                        "INSERT INTO game_evidence "
                        "(evidence_id,tenant_id,actor_id,content_hash,command_id,"
                        "recorded_at,evidence_json) VALUES "
                        "('evidence_int2_stray_build',:tenant_id,:actor_id,"
                        ":content_hash,:command_id,:recorded_at,CAST('{}' AS jsonb))"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "actor_id": actor_id,
                        "content_hash": content_ref["content_hash"],
                        "command_id": command_id,
                        "recorded_at": completed_at,
                    },
                )
            elif corrupt == "launch_inactive":
                await connection.execute(
                    text(
                        "UPDATE launch_authorities SET active=false "
                        "WHERE tenant_id=:tenant_id AND actor_id=:actor_id"
                    ),
                    {"tenant_id": tenant_id, "actor_id": actor_id},
                )
            elif corrupt == "policy_inactive":
                await connection.execute(
                    text(
                        "UPDATE build_policies SET active=false "
                        "WHERE tenant_id=:tenant_id AND actor_id=:actor_id"
                    ),
                    {"tenant_id": tenant_id, "actor_id": actor_id},
                )
            elif corrupt == "policy_hash":
                await connection.execute(
                    text(
                        "UPDATE build_policies SET policy_json = policy_json || "
                        "CAST(:policy_drift AS jsonb) "
                        "WHERE tenant_id=:tenant_id AND actor_id=:actor_id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "actor_id": actor_id,
                        "policy_drift": _json({"unexpected": True}),
                    },
                )
            elif corrupt == "policy_schema":
                policy_json = (
                    await connection.execute(
                        text(
                            "SELECT policy_json FROM build_policies "
                            "WHERE tenant_id=:tenant_id AND actor_id=:actor_id"
                        ),
                        {"tenant_id": tenant_id, "actor_id": actor_id},
                    )
                ).scalar_one()
                invalid_policy = dict(policy_json)
                invalid_policy["parameter_schema"] = {"type": 5}
                await connection.execute(
                    text(
                        "UPDATE build_policies SET policy_json=CAST(:policy_json AS jsonb),"
                        "policy_sha256=:policy_sha256 "
                        "WHERE tenant_id=:tenant_id AND actor_id=:actor_id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "actor_id": actor_id,
                        "policy_json": _json(invalid_policy),
                        "policy_sha256": canonical_json_sha256(invalid_policy),
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO idempotency_receipts "
                    "(tenant_id,actor_id,operation,idempotency_key,request_sha256,"
                    "command_id,accepted_at) VALUES "
                    "(:tenant_id,:actor_id,'CREATE_SKILL_BUILD','idem_int2_legacy_build',"
                    ":request_sha256,:command_id,:accepted_at)"
                ),
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "request_sha256": (
                        "9" * 64 if corrupt == "idempotency_request" else request_sha256
                    ),
                    "command_id": command_id,
                    "accepted_at": accepted_at,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO workflow_jobs "
                    "(job_id,tenant_id,command_id,operation,subject_type,subject_id,phase,"
                    "status,attempt,fencing_token,lease_owner,lease_expires_at,next_attempt_at,"
                    "request_sha256,job_json,last_error_json,created_at,updated_at) VALUES "
                    "(:job_id,:tenant_id,:command_id,'CREATE_SKILL_BUILD','SKILL_BUILD',"
                    ":build_id,'VALIDATE_SOURCE','FAILED',1,:fencing_token,NULL,NULL,NULL,"
                    ":request_sha256,"
                    "CAST(:job_json AS jsonb),CAST(:last_error_json AS jsonb),"
                    ":accepted_at,:updated_at)"
                ),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "command_id": command_id,
                    "build_id": build_id,
                    "request_sha256": request_sha256,
                    "fencing_token": 2 if corrupt == "job_fence" else 1,
                    "job_json": _json(job),
                    "last_error_json": _json(build_error),
                    "accepted_at": accepted_at,
                    "updated_at": completed_at,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO skill_builds "
                    "(build_id,tenant_id,actor_id,command_id,skill_id,status,terminal,"
                    "created_at,updated_at,build_json,request_json) VALUES "
                    "(:build_id,:tenant_id,:actor_id,:command_id,:skill_id,'REJECTED',true,"
                    ":accepted_at,:updated_at,CAST(:build_json AS jsonb),"
                    "CAST(:request_json AS jsonb))"
                ),
                {
                    "build_id": build_id,
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "command_id": command_id,
                    "skill_id": request["skill_id"],
                    "accepted_at": accepted_at,
                    "updated_at": completed_at,
                    "build_json": _json(build),
                    "request_json": _json(request),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO job_step_receipts "
                    "(receipt_id,tenant_id,job_id,step_name,fencing_token,input_sha256,"
                    "output_sha256,receipt_json,completed_at) VALUES "
                    "(:receipt_id,:tenant_id,:job_id,'BUILD_REJECTED',1,:input_sha256,"
                    ":output_sha256,CAST(:receipt_json AS jsonb),:completed_at)"
                ),
                {
                    "receipt_id": receipt_id,
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                    "input_sha256": request_sha256,
                    "output_sha256": canonical_json_sha256(receipt_output),
                    "receipt_json": _json(receipt_output),
                    "completed_at": completed_at,
                },
            )
    finally:
        await engine.dispose()


async def _seed_accepted_legacy_build(
    database_url: str,
    *,
    corrupt: str,
) -> None:
    await _seed_rejected_legacy_build(database_url, corrupt="none")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            build_row = (
                await connection.execute(
                    text(
                        "SELECT build_id,created_at,build_json FROM skill_builds"
                    )
                )
            ).mappings().one()
            command_row = (
                await connection.execute(
                    text(
                        "SELECT command_id,accepted_at,record_json FROM commands"
                    )
                )
            ).mappings().one()
            accepted_at = command_row["accepted_at"]
            accepted_wire = accepted_at.isoformat().replace("+00:00", "Z")
            accepted_command_wire = accepted_at.isoformat()
            build = dict(build_row["build_json"])
            build.update(
                {
                    "status": "ACCEPTED",
                    "terminal": False,
                    "updated_at": accepted_wire,
                    "skill_version_id": None,
                    "artifact": None,
                    "certification": None,
                    "phases": [
                        {
                            "name": phase,
                            "status": "PENDING",
                            "started_at": None,
                            "finished_at": None,
                            "diagnostic_codes": [],
                        }
                        for phase in (
                            "VALIDATE_SOURCE",
                            "COMPILE",
                            "PUBLIC_TEST",
                            "HIDDEN_TEST",
                            "CERTIFY",
                        )
                    ],
                    "failure": None,
                    "evidence_refs": [],
                }
            )
            command = dict(command_row["record_json"])
            command.update(
                {
                    "status": "ACCEPTED",
                    "stage": "ACCEPT",
                    "terminal": False,
                    "updated_at": accepted_command_wire,
                    "result": None,
                    "error": None,
                    "evidence_refs": [],
                    "revision": 1,
                }
            )
            command_columns = {
                "status": "ACCEPTED",
                "revision": 1,
                "terminal": False,
            }
            if corrupt == "terminal_command":
                command.update(
                    {
                        "status": "APPLIED",
                        "stage": "COMPLETE",
                        "terminal": True,
                        "result": {
                            "result_type": "RESOURCE_CREATED",
                            "resource_type": "SKILL_BUILD",
                            "resource_id": build_row["build_id"],
                            "resource_url": f"/v1/skill-builds/{build_row['build_id']}",
                        },
                        "revision": 2,
                    }
                )
                command_columns = {
                    "status": "APPLIED",
                    "revision": 2,
                    "terminal": True,
                }
            await connection.execute(text("DELETE FROM job_step_receipts"))
            await connection.execute(
                text(
                    "UPDATE skill_builds SET status='ACCEPTED',terminal=false,"
                    "updated_at=:accepted_at,build_json=CAST(:build AS jsonb)"
                ),
                {"accepted_at": accepted_at, "build": _json(build)},
            )
            await connection.execute(
                text(
                    "UPDATE commands SET status=:status,revision=:revision,"
                    "terminal=:terminal,updated_at=:accepted_at,"
                    "record_json=CAST(:command AS jsonb)"
                ),
                {
                    **command_columns,
                    "accepted_at": accepted_at,
                    "command": _json(command),
                },
            )
            if corrupt == "terminal_job":
                await connection.execute(
                    text(
                        "UPDATE workflow_jobs SET phase='COMPLETE',status='SUCCEEDED',"
                        "attempt=1,fencing_token=1,lease_owner=NULL,lease_expires_at=NULL,"
                        "next_attempt_at=NULL,last_error_json=NULL,updated_at=:accepted_at"
                    ),
                    {"accepted_at": accepted_at},
                )
            else:
                await connection.execute(
                    text(
                        "UPDATE workflow_jobs SET phase='ACCEPT',status='READY',attempt=0,"
                        "fencing_token=0,lease_owner=NULL,lease_expires_at=NULL,"
                        "next_attempt_at=:accepted_at,last_error_json=NULL,"
                        "updated_at=:accepted_at"
                    ),
                    {"accepted_at": accepted_at},
                )
    finally:
        await engine.dispose()


async def _seed_certified_legacy_build(
    database_url: str,
    *,
    corrupt: str = "none",
) -> None:
    """Materialize the exact v0.4 successful Build closure without INT2 tables."""

    await _seed_rejected_legacy_build(database_url, corrupt="none")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            build_row = (
                await connection.execute(
                    text(
                        "SELECT build_id,tenant_id,actor_id,command_id,skill_id,created_at,"
                        "updated_at,build_json,request_json FROM skill_builds"
                    )
                )
            ).mappings().one()
            command_row = (
                await connection.execute(
                    text("SELECT revision,record_json FROM commands")
                )
            ).mappings().one()
            job_row = (
                await connection.execute(
                    text(
                        "SELECT job_id,request_sha256,fencing_token,job_json "
                        "FROM workflow_jobs"
                    )
                )
            ).mappings().one()
            launch = (
                await connection.execute(
                    text(
                        "SELECT learner_id,world_id,build_policy_id FROM launch_authorities "
                        "WHERE tenant_id=:tenant_id AND actor_id=:actor_id AND active"
                    ),
                    dict(build_row),
                )
            ).mappings().one()
            policy = (
                await connection.execute(
                    text(
                        "SELECT compiler_profile,compiler_version,test_suite_version,"
                        "policy_json,policy_sha256 FROM build_policies "
                        "WHERE tenant_id=:tenant_id AND actor_id=:actor_id "
                        "AND build_policy_id=:build_policy_id AND active"
                    ),
                    {**dict(build_row), **dict(launch)},
                )
            ).mappings().one()

            build = dict(build_row["build_json"])
            request = dict(build_row["request_json"])
            command = dict(command_row["record_json"])
            completed_at = build_row["updated_at"]
            completed_wire = completed_at.isoformat().replace("+00:00", "Z")
            started_wire = build_row["created_at"].isoformat().replace("+00:00", "Z")
            source_sha256 = canonical_source_bundle_sha256(request["source_bundle"])
            artifact_sha256 = hashlib.sha256(b"legacy-certified-artifact").hexdigest()
            build_identity = hashlib.sha256(b"legacy-certified-identity").hexdigest()
            skill_version_id = _scoped_id(
                "skillver", build_row["build_id"], artifact_sha256
            )
            certification_id = _scoped_id(
                "cert", build_row["build_id"], artifact_sha256
            )
            evidence_id = _scoped_id("evidence", "build", build_row["build_id"])
            certified_schema, certified_schema_sha256 = certified_parameter_schema(
                policy["policy_json"],
                policy_sha256=policy["policy_sha256"],
                build_id=build_row["build_id"],
                skill_id=build_row["skill_id"],
                skill_version_id=skill_version_id,
                source_sha256=source_sha256,
                artifact_sha256=artifact_sha256,
                certification_id=certification_id,
                build_policy_id=launch["build_policy_id"],
                actor_id=build_row["actor_id"],
                content_hash=build["request_context"]["content_ref"]["content_hash"],
                capabilities=request["requested_capabilities"],
            )
            if corrupt == "empty_certification_tuple":
                skill_version_id = ""
                certified_schema = {
                    **certified_schema,
                    "x-yaya-certification": {
                        **certified_schema["x-yaya-certification"],
                        "skill_version_id": "",
                    },
                }
                certified_schema_sha256 = canonical_json_sha256(certified_schema)
            evidence_payload = {
                "evidence_kind": "BUILD_CERTIFICATION",
                "build_id": build_row["build_id"],
                "skill_id": build_row["skill_id"],
                "skill_version_id": skill_version_id,
                "artifact_sha256": artifact_sha256,
                "test_suite_version": policy["test_suite_version"],
                "outcome": "CERTIFIED",
            }
            evidence_sha256 = canonical_json_sha256(evidence_payload)
            evidence_ref = {
                "evidence_id": evidence_id,
                "evidence_type": "TEST_REPORT",
                "created_at": completed_wire,
                "sha256": evidence_sha256,
                "uri": f"/v1/evidence/{evidence_id}",
            }
            command_evidence_ref = {
                **evidence_ref,
                "created_at": completed_at.isoformat(),
            }
            versions = dict(build["versions"])
            versions.update(
                {
                    "policy_version": launch["build_policy_id"],
                    "skill_version": skill_version_id,
                    "artifact_sha256": artifact_sha256,
                    "compiler_version": policy["compiler_version"],
                    "sandbox_image_digest": policy["policy_json"]["compiler_image"],
                    "test_suite_version": policy["test_suite_version"],
                }
            )
            metadata = {
                "schema_version": "1.0.0",
                "artifact_sha256": artifact_sha256,
                "source_sha256": source_sha256,
                "build_identity": build_identity,
                "size_bytes": 25,
                "compiler_profile": policy["compiler_profile"],
                "compiler_version": policy["compiler_version"],
                "compiler_image": policy["policy_json"]["compiler_image"],
                "test_suite_version": policy["test_suite_version"],
                "policy_sha256": policy["policy_sha256"],
                "parameter_schema": certified_schema,
                "parameter_schema_sha256": certified_schema_sha256,
            }
            certification = {
                "schema_version": "1.0.0",
                "certification_id": certification_id,
                "build_id": build_row["build_id"],
                "skill_id": build_row["skill_id"],
                "skill_version_id": skill_version_id,
                "artifact_sha256": artifact_sha256,
                "source_sha256": source_sha256,
                "actor_id": build_row["actor_id"],
                "content_hash": build["request_context"]["content_ref"]["content_hash"],
                "build_policy_id": launch["build_policy_id"],
                "policy_sha256": policy["policy_sha256"],
                "capabilities": request["requested_capabilities"],
                "issued_at": completed_wire,
                "parameter_schema": certified_schema,
                "parameter_schema_sha256": certified_schema_sha256,
            }
            evidence = {
                "request_context": build["request_context"],
                "evidence_ref": evidence_ref,
                "subject": {"learner_id": launch["learner_id"]},
                "source": {
                    "source_type": "SKILL_BUILD",
                    "source_id": build_row["build_id"],
                    "command_id": build_row["command_id"],
                    "world_id": launch["world_id"],
                },
                "occurred_at": completed_wire,
                "recorded_at": completed_wire,
                "integrity": {
                    "payload_sha256": evidence_sha256,
                    "previous_evidence_sha256": None,
                },
                "payload": evidence_payload,
                "related_evidence": [],
                "versions": versions,
            }
            if corrupt == "evidence_payload":
                evidence["payload"] = {**evidence_payload, "outcome": "REJECTED"}
            if corrupt == "artifact_metadata":
                metadata["size_bytes"] = 0
            if corrupt == "cert_schema":
                certification["capabilities"] = []
            build.update(
                {
                    "skill_version_id": skill_version_id,
                    "status": "CERTIFIED",
                    "terminal": True,
                    "artifact": {
                        "artifact_sha256": artifact_sha256,
                        "source_sha256": source_sha256,
                        "compiler_profile": policy["compiler_profile"],
                        "compiler_version": policy["compiler_version"],
                        "test_suite_version": policy["test_suite_version"],
                    },
                    "certification": {
                        "certification_id": certification_id,
                        "issued_at": completed_wire,
                        "capabilities": request["requested_capabilities"],
                    },
                    "phases": [
                        {
                            "name": phase,
                            "status": "PASSED",
                            "started_at": started_wire,
                            "finished_at": completed_wire,
                            "diagnostic_codes": [],
                        }
                        for phase in (
                            "VALIDATE_SOURCE",
                            "COMPILE",
                            "PUBLIC_TEST",
                            "HIDDEN_TEST",
                            "CERTIFY",
                        )
                    ],
                    "failure": None,
                    "evidence_refs": [evidence_ref],
                    "versions": versions,
                }
            )
            command.update(
                {
                    "status": "APPLIED",
                    "stage": "COMPLETE",
                    "terminal": True,
                    "updated_at": completed_at.isoformat(),
                    "result": {
                        "result_type": "RESOURCE_CREATED",
                        "resource_type": "SKILL_BUILD",
                        "resource_id": build_row["build_id"],
                        "resource_url": f"/v1/skill-builds/{build_row['build_id']}",
                    },
                    "error": None,
                    "evidence_refs": [command_evidence_ref],
                }
            )
            if corrupt == "build_artifact_projection":
                build["artifact"]["compiler_version"] = "drifted-compiler"
            if corrupt == "empty_capability":
                request["requested_capabilities"] = [""]
                decorated_schema = {
                    **certified_schema,
                    "x-yaya-certification": {
                        **certified_schema["x-yaya-certification"],
                        "capabilities": [""],
                    },
                }
                decorated_schema_sha256 = canonical_json_sha256(decorated_schema)
                metadata["parameter_schema"] = decorated_schema
                metadata["parameter_schema_sha256"] = decorated_schema_sha256
                certification["capabilities"] = [""]
                certification["parameter_schema"] = decorated_schema
                certification["parameter_schema_sha256"] = decorated_schema_sha256
                build["certification"]["capabilities"] = [""]
            receipt_output = {
                "build_id": build_row["build_id"],
                "skill_version_id": skill_version_id,
                "artifact_sha256": artifact_sha256,
                "certification_id": certification_id,
                "evidence_id": evidence_id,
                "build_identity": build_identity,
            }

            await connection.execute(text("DELETE FROM job_step_receipts"))
            await connection.execute(
                text(
                    "UPDATE commands SET status='APPLIED',terminal=true,"
                    "record_json=CAST(:record_json AS jsonb) WHERE command_id=:command_id"
                ),
                {"command_id": build_row["command_id"], "record_json": _json(command)},
            )
            await connection.execute(
                text(
                    "UPDATE workflow_jobs SET phase=:phase,status='SUCCEEDED',"
                    "lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=NULL,"
                    "last_error_json=NULL,job_json=CAST(:job_json AS jsonb) "
                    "WHERE job_id=:job_id"
                ),
                {
                    "job_id": job_row["job_id"],
                    "phase": (
                        "CERTIFY" if corrupt == "terminal_job_phase" else "COMPLETE"
                    ),
                    "job_json": _json(
                        {**job_row["job_json"], "request": request}
                    ),
                },
            )
            await connection.execute(
                text(
                    "UPDATE skill_builds SET status='CERTIFIED',terminal=true,"
                    "build_json=CAST(:build_json AS jsonb),"
                    "request_json=CAST(:request_json AS jsonb) WHERE build_id=:build_id"
                ),
                {
                    "build_id": build_row["build_id"],
                    "build_json": _json(build),
                    "request_json": _json(request),
                },
            )
            if corrupt == "empty_capability":
                await connection.execute(
                    text(
                        "UPDATE build_policies SET "
                        "allowed_capabilities=CAST(:capabilities AS jsonb) "
                        "WHERE tenant_id=:tenant_id AND actor_id=:actor_id"
                    ),
                    {
                        **dict(build_row),
                        "capabilities": _json([""]),
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO skill_artifacts "
                    "(tenant_id,artifact_sha256,build_id,actor_id,content_hash,skill_id,"
                    "source_sha256,artifact_uri,metadata_json,created_at) VALUES "
                    "(:tenant_id,:artifact_sha256,:build_id,:actor_id,:content_hash,:skill_id,"
                    ":source_sha256,:artifact_uri,CAST(:metadata_json AS jsonb),:created_at)"
                ),
                {
                    **dict(build_row),
                    "artifact_sha256": artifact_sha256,
                    "content_hash": build["request_context"]["content_ref"]["content_hash"],
                    "source_sha256": source_sha256,
                    "artifact_uri": f"artifact://sha256/{artifact_sha256}",
                    "metadata_json": _json(metadata),
                    "created_at": completed_at,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO skill_certifications "
                    "(certification_id,tenant_id,build_id,skill_id,skill_version_id,"
                    "artifact_sha256,actor_id,content_hash,certification_sha256,"
                    "certification_json,certified_at) VALUES "
                    "(:certification_id,:tenant_id,:build_id,:skill_id,:skill_version_id,"
                    ":artifact_sha256,:actor_id,:content_hash,:certification_sha256,"
                    "CAST(:certification_json AS jsonb),:certified_at)"
                ),
                {
                    **dict(build_row),
                    "certification_id": certification_id,
                    "skill_version_id": skill_version_id,
                    "artifact_sha256": artifact_sha256,
                    "content_hash": build["request_context"]["content_ref"]["content_hash"],
                    "certification_sha256": canonical_json_sha256(certification),
                    "certification_json": _json(certification),
                    "certified_at": completed_at,
                },
            )
            evidence_values = {
                **dict(build_row),
                "evidence_id": evidence_id,
                "content_hash": build["request_context"]["content_ref"]["content_hash"],
                "recorded_at": completed_at,
                "evidence_json": _json(evidence),
            }
            if corrupt != "missing_evidence":
                if corrupt == "wrong_evidence_command":
                    evidence_values["command_id"] = "cmd_int2_wrong_certified_evidence"
                await connection.execute(
                    text(
                        "INSERT INTO game_evidence "
                        "(evidence_id,tenant_id,actor_id,content_hash,command_id,recorded_at,"
                        "evidence_json) VALUES (:evidence_id,:tenant_id,:actor_id,:content_hash,"
                        ":command_id,:recorded_at,CAST(:evidence_json AS jsonb))"
                    ),
                    evidence_values,
                )
            if corrupt == "duplicate_evidence":
                await connection.execute(
                    text(
                        "INSERT INTO game_evidence "
                        "(evidence_id,tenant_id,actor_id,content_hash,command_id,recorded_at,"
                        "evidence_json) VALUES ('evidence_int2_duplicate_certified',:tenant_id,"
                        ":actor_id,:content_hash,:command_id,:recorded_at,"
                        "CAST(:evidence_json AS jsonb))"
                    ),
                    evidence_values,
                )
            await connection.execute(
                text(
                    "INSERT INTO job_step_receipts "
                    "(receipt_id,tenant_id,job_id,step_name,fencing_token,input_sha256,"
                    "output_sha256,receipt_json,completed_at) VALUES "
                    "(:receipt_id,:tenant_id,:job_id,'BUILD_CERTIFIED',:fencing_token,"
                    ":input_sha256,:output_sha256,CAST(:receipt_json AS jsonb),:completed_at)"
                ),
                {
                    **dict(build_row),
                    **dict(job_row),
                    "receipt_id": (
                        "receipt_int2_wrong_certified_identity"
                        if corrupt == "terminal_receipt_identity"
                        else workflow_step_receipt_id(
                            build_row["tenant_id"],
                            job_row["job_id"],
                            "BUILD_CERTIFIED",
                        )
                    ),
                    "input_sha256": job_row["request_sha256"],
                    "output_sha256": canonical_json_sha256(receipt_output),
                    "receipt_json": _json(receipt_output),
                    "completed_at": completed_at,
                },
            )
    finally:
        await engine.dispose()


async def _legacy_build_state(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            build = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT build_id,tenant_id,actor_id,command_id,skill_id,status,"
                            "terminal,created_at,updated_at,build_json,request_json "
                            "FROM skill_builds"
                        )
                    )
                ).mappings().one()
            )
            command = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT command_id,tenant_id,actor_id,command_type,status,revision,"
                            "terminal,accepted_at,updated_at,record_json FROM commands"
                        )
                    )
                ).mappings().one()
            )
            job = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT job_id,tenant_id,command_id,operation,subject_type,subject_id,"
                            "phase,status,attempt,fencing_token,lease_owner,lease_expires_at,"
                            "next_attempt_at,request_sha256,job_json,last_error_json,created_at,"
                            "updated_at FROM workflow_jobs"
                        )
                    )
                ).mappings().one()
            )
            receipt_row = (
                await connection.execute(
                    text(
                        "SELECT receipt_id,tenant_id,job_id,step_name,fencing_token,"
                        "input_sha256,output_sha256,receipt_json,completed_at "
                        "FROM job_step_receipts"
                    )
                )
            ).mappings().one_or_none()
            receipt = dict(receipt_row) if receipt_row is not None else None
            idempotency = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT tenant_id,actor_id,operation,idempotency_key,request_sha256,"
                            "command_id,accepted_at FROM idempotency_receipts"
                        )
                    )
                ).mappings().one()
            )
            artifacts = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT tenant_id,artifact_sha256,build_id,actor_id,content_hash,"
                            "skill_id,source_sha256,artifact_uri,metadata_json,created_at "
                            "FROM skill_artifacts ORDER BY tenant_id,artifact_sha256"
                        )
                    )
                ).mappings()
            ]
            certifications = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT certification_id,tenant_id,build_id,skill_id,"
                            "skill_version_id,artifact_sha256,actor_id,content_hash,"
                            "certification_sha256,certification_json,certified_at "
                            "FROM skill_certifications ORDER BY certification_id"
                        )
                    )
                ).mappings()
            ]
            evidence = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT evidence_id,tenant_id,actor_id,content_hash,command_id,"
                            "recorded_at,evidence_json FROM game_evidence ORDER BY evidence_id"
                        )
                    )
                ).mappings()
            ]
            provenance_table = (
                await connection.execute(
                    text("SELECT to_regclass('skill_build_provenance')")
                )
            ).scalar_one()
            provenance_count = None
            marker_count = None
            provenance_sha256 = None
            terminal_count = None
            terminal_sha256 = None
            certification_provenance_count = None
            certification_provenance_sha256 = None
            if provenance_table is not None:
                provenance = (
                    await connection.execute(
                        text(
                            "SELECT authority_sha256 FROM skill_build_provenance"
                        )
                    )
                ).mappings().one()
                provenance_count = 1
                provenance_sha256 = provenance["authority_sha256"]
                marker_count = (
                    await connection.execute(
                        text("SELECT count(*) FROM int2_legacy_build_markers")
                    )
                ).scalar_one()
                terminal_rows = (
                    await connection.execute(
                        text(
                            "SELECT authority_sha256 FROM "
                            "skill_build_terminal_authority"
                        )
                    )
                ).mappings().all()
                terminal_count = len(terminal_rows)
                terminal_sha256 = (
                    terminal_rows[0]["authority_sha256"]
                    if len(terminal_rows) == 1
                    else None
                )
                certification_provenance_rows = (
                    await connection.execute(
                        text(
                            "SELECT authority_sha256 FROM "
                            "skill_certification_provenance ORDER BY certification_id"
                        )
                    )
                ).mappings().all()
                certification_provenance_count = len(certification_provenance_rows)
                certification_provenance_sha256 = (
                    certification_provenance_rows[0]["authority_sha256"]
                    if len(certification_provenance_rows) == 1
                    else None
                )
            return {
                "revision": revision,
                "build": build,
                "command": command,
                "job": job,
                "receipt": receipt,
                "idempotency": idempotency,
                "artifacts": artifacts,
                "certifications": certifications,
                "evidence": evidence,
                "provenance_count": provenance_count,
                "marker_count": marker_count,
                "provenance_sha256": provenance_sha256,
                "terminal_count": terminal_count,
                "terminal_sha256": terminal_sha256,
                "certification_provenance_count": certification_provenance_count,
                "certification_provenance_sha256": certification_provenance_sha256,
            }
    finally:
        await engine.dispose()


async def _read_legacy_build(database_url: str) -> dict[str, Any]:
    tenant_id = "tenant_int2_legacy_build"
    actor_id = "learner_int2_legacy_build"
    command_id = "cmd_int2_legacy_build"
    build_id = "build_" + hashlib.sha256(command_id.encode()).hexdigest()[:24]
    sessions = create_session_factory(database_url)
    try:
        store = PostgresSkillBuildStore(
            sessions,
            PostgresCommandStore(sessions),
        )
        context = OperationContext(
            request_id="req_int2_legacy_build_read",
            correlation_id="corr_int2_legacy_build_read",
            trace_id="trace_int2_legacy_build_read",
            requested_at=datetime.now(UTC),
            actor=ActorRef(
                tenant_id=tenant_id,
                actor_id=actor_id,
                actor_type=ActorType.STUDENT,
                roles=("game:player",),
            ),
            content_ref=ContentRef(
                unit_id="CONTENT_INT2_LEGACY_BUILD",
                version="1.0.0",
                content_hash="1" * 64,
            ),
            command_id="cmd_int2_legacy_build_read",
            causation_id=None,
        )
        result = await store.get(build_id, context)
        if not isinstance(result, Success):
            raise AssertionError(result.error)
        return result.value
    finally:
        await sessions.kw["bind"].dispose()


async def _assert_legacy_certification_valid(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            certification = await session.scalar(select(SkillCertificationRow))
            assert certification is not None
            assert await validate_certification_authority(session, certification) is not None
    finally:
        await sessions.kw["bind"].dispose()


async def _corrupt_certified_authority(database_url: str, *, corrupt: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            if corrupt == "evidence_payload":
                evidence = dict(
                    (
                        await connection.execute(
                            text("SELECT evidence_json FROM game_evidence")
                        )
                    ).scalar_one()
                )
                evidence["payload"] = {**evidence["payload"], "outcome": "REJECTED"}
                await connection.execute(
                    text(
                        "UPDATE game_evidence SET evidence_json=CAST(:value AS jsonb)"
                    ),
                    {"value": _json(evidence)},
                )
            elif corrupt == "artifact_metadata":
                metadata = dict(
                    (
                        await connection.execute(
                            text("SELECT metadata_json FROM skill_artifacts")
                        )
                    ).scalar_one()
                )
                metadata["size_bytes"] = 0
                await connection.execute(
                    text(
                        "UPDATE skill_artifacts SET metadata_json=CAST(:value AS jsonb)"
                    ),
                    {"value": _json(metadata)},
                )
            elif corrupt == "cert_schema":
                certification = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT certification_json FROM skill_certifications"
                            )
                        )
                    ).scalar_one()
                )
                certification["capabilities"] = []
                await connection.execute(
                    text(
                        "UPDATE skill_certifications SET "
                        "certification_json=CAST(:value AS jsonb),"
                        "certification_sha256=:sha256"
                    ),
                    {
                        "value": _json(certification),
                        "sha256": canonical_json_sha256(certification),
                    },
                )
            else:
                raise AssertionError(f"unknown certified authority corruption: {corrupt}")
    finally:
        await engine.dispose()


async def _rewrite_rejected_build_request_authority(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE workflow_jobs SET request_sha256=:sha "
                    "WHERE operation='CREATE_SKILL_BUILD'"
                ),
                {"sha": "a" * 64},
            )
            await connection.execute(
                text(
                    "UPDATE job_step_receipts SET input_sha256=:sha "
                    "WHERE step_name='BUILD_REJECTED'"
                ),
                {"sha": "a" * 64},
            )
    finally:
        await engine.dispose()


def _scoped_id(prefix: str, *parts: str) -> str:
    framed = "\0".join((prefix, *parts)).encode()
    return f"{prefix}_{hashlib.sha256(framed).hexdigest()[:24]}"


async def _seed_orphan_legacy_learner_projection(
    database_url: str,
    *,
    corrupt: str,
    include_assistance: bool = False,
) -> None:
    now = datetime(2026, 8, 14, 3, 4, 5, 678901, tzinfo=UTC)
    tenant_id = "tenant_int2_orphan_learner"
    actor_id = "learner_int2_orphan"
    command_id = "command_int2_orphan_learner"
    job_id = "job_int2_orphan_learner"
    event_id = "event_int2_orphan_learner"
    content_hash = "3" * 64
    objective = {"schema_version": "1.0.0", "source_evidence_ids": []}
    if include_assistance:
        objective["assistance"] = {
            "authority_version": "1.0.0",
            "assistance_authority": "NONE",
            "used_skill_patch": False,
        }
    request_sha256 = canonical_json_sha256(objective)
    if corrupt == "request_sha256":
        request_sha256 = "a" * 64
    terminal = corrupt in {"result_sha256", "receipt_identity"}
    receipt_json: dict[str, Any] = {}
    receipt_id = "receipt_" + hashlib.sha256(
        "\0".join(
            (
                "receipt",
                tenant_id,
                job_id,
                "LEARNER_PROJECTION_COMMITTED",
            )
        ).encode()
    ).hexdigest()[:24]
    if corrupt == "receipt_identity":
        receipt_id = "receipt_int2_invalid_identity"
    receipt_wire = {
        "receipt_id": receipt_id,
        "step_name": "LEARNER_PROJECTION_COMMITTED",
        "fencing_token": 1,
        "input_sha256": request_sha256,
        "output_sha256": canonical_json_sha256(receipt_json),
        "receipt_json": receipt_json,
        "completed_at": now.isoformat().replace("+00:00", "Z"),
    }
    result_json = {"projection_receipt": receipt_wire} if terminal else None
    result_sha256 = (
        "b" * 64
        if corrupt == "result_sha256"
        else canonical_json_sha256(result_json)
        if result_json is not None
        else None
    )
    command = {
        "command_id": command_id,
        "command_type": "EXECUTE_AGENT_TURN",
        "status": "APPLIED",
        "revision": 1,
        "terminal": True,
    }
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO commands "
                    "(command_id,tenant_id,actor_id,command_type,status,revision,terminal,"
                    "accepted_at,updated_at,record_json) VALUES "
                    "(:command_id,:tenant_id,:actor_id,'EXECUTE_AGENT_TURN','APPLIED',1,true,"
                    ":now,:now,CAST(:record_json AS jsonb))"
                ),
                {
                    "command_id": command_id,
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "now": now,
                    "record_json": _json(command),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO workflow_jobs "
                    "(job_id,tenant_id,command_id,operation,subject_type,subject_id,phase,"
                    "status,attempt,fencing_token,lease_owner,lease_expires_at,next_attempt_at,"
                    "request_sha256,job_json,last_error_json,created_at,updated_at) VALUES "
                    "(:job_id,:tenant_id,:command_id,'EXECUTE_AGENT_TURN','AGENT_TURN',"
                    "'turn_int2_orphan',:phase,:status,0,:fencing_token,NULL,NULL,"
                    ":next_attempt_at,:request_sha256,CAST(:job_json AS jsonb),NULL,:now,:now)"
                ),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "command_id": command_id,
                    "phase": "COMPLETE" if terminal else "LEARNER_PROJECTION",
                    "status": "SUCCEEDED" if terminal else "WAITING_PROJECTION",
                    "fencing_token": 1 if terminal else 0,
                    "next_attempt_at": None,
                    "request_sha256": request_sha256,
                    "job_json": _json({"schema_version": "1.0.0"}),
                    "now": now,
                },
            )
            profile = {
                "learner_id": actor_id,
                "revision": 0,
                "projected_through_sequence": 0,
            }
            await connection.execute(
                text(
                    "INSERT INTO learner_profiles "
                    "(tenant_id,learner_id,actor_id,content_hash,profile_sha256,"
                    "profile_json,created_at,updated_at) VALUES "
                    "(:tenant_id,:learner_id,:actor_id,:content_hash,:profile_sha256,"
                    "CAST(:profile_json AS jsonb),:now,:now)"
                ),
                {
                    "tenant_id": tenant_id,
                    "learner_id": actor_id,
                    "actor_id": actor_id,
                    "content_hash": content_hash,
                    "profile_sha256": canonical_json_sha256(profile),
                    "profile_json": _json(profile),
                    "now": now,
                },
            )
            event = {
                "event_id": event_id,
                "event_type": "agent.turn.feedback.recorded",
                "event_version": 1,
                "stream_id": "agent-session:session_int2_orphan",
                "sequence": 1,
                "occurred_at": now.isoformat().replace("+00:00", "Z"),
            }
            await connection.execute(
                text(
                    "INSERT INTO domain_events "
                    "(event_id,tenant_id,stream_id,sequence,occurred_at,event_json) VALUES "
                    "(:event_id,:tenant_id,:stream_id,1,:now,CAST(:event_json AS jsonb))"
                ),
                {
                    "event_id": event_id,
                    "tenant_id": tenant_id,
                    "stream_id": event["stream_id"],
                    "now": now,
                    "event_json": _json(event),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO learner_projection_jobs "
                    "(job_id,tenant_id,command_id,session_id,turn_id,run_id,learner_id,actor_id,"
                    "content_hash,source_event_id,expected_revision,through_sequence,projection_json,"
                    "status,attempt,fencing_token,lease_owner,lease_expires_at,next_attempt_at,"
                    "request_sha256,result_sha256,result_json,last_error_json,completed_at,"
                    "created_at,updated_at) VALUES "
                    "(:job_id,:tenant_id,:command_id,'session_int2_orphan','turn_int2_orphan',"
                    "'run_int2_orphan_missing',:learner_id,:actor_id,:content_hash,:source_event_id,"
                    "0,1,CAST(:projection_json AS jsonb),:status,:attempt,:fencing_token,"
                    "NULL,NULL,:next_attempt_at,:request_sha256,:result_sha256,"
                    "CAST(:result_json AS jsonb),NULL,:completed_at,:now,:now)"
                ),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "command_id": command_id,
                    "learner_id": actor_id,
                    "actor_id": actor_id,
                    "content_hash": content_hash,
                    "source_event_id": event_id,
                    "projection_json": _json(objective),
                    "status": "SUCCEEDED" if terminal else "READY",
                    "attempt": 1 if terminal else 0,
                    "fencing_token": 1 if terminal else 0,
                    "next_attempt_at": None if terminal else now,
                    "request_sha256": request_sha256,
                    "result_sha256": result_sha256,
                    "result_json": _json(result_json) if result_json is not None else None,
                    "completed_at": now if terminal else None,
                    "now": now,
                },
            )
            if terminal:
                await connection.execute(
                    text(
                        "INSERT INTO job_step_receipts "
                        "(receipt_id,tenant_id,job_id,step_name,fencing_token,input_sha256,"
                        "output_sha256,receipt_json,completed_at) VALUES "
                        "(:receipt_id,:tenant_id,:job_id,'LEARNER_PROJECTION_COMMITTED',1,"
                        ":input_sha256,:output_sha256,CAST(:receipt_json AS jsonb),:now)"
                    ),
                    {
                        "receipt_id": receipt_id,
                        "tenant_id": tenant_id,
                        "job_id": job_id,
                        "input_sha256": request_sha256,
                        "output_sha256": canonical_json_sha256(receipt_json),
                        "receipt_json": _json(receipt_json),
                        "now": now,
                    },
                )
    finally:
        await engine.dispose()


async def _seed_legacy_draft(
    database_url: str,
    *,
    revision: int = 1,
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    created_at = datetime(2026, 8, 14, 1, 2, 3, 456789, tzinfo=UTC)
    updated_at = updated_at or created_at
    content = {
        "unit_id": "content_int2_migration",
        "version": "1.0.0",
        "content_hash": "1" * 64,
    }
    actor = {
        "tenant_id": "tenant_int2_migration",
        "actor_id": "learner_int2_migration",
        "actor_type": "student",
        "roles": ["game:player"],
    }
    request_context = {
        "request_id": "request_int2_migration",
        "trace_id": "trace_int2_migration",
        "schema_version": "1.0.0",
        "actor": actor,
        "content_ref": content,
        "client_timestamp": None,
    }
    source_text = "int main() { return 0; }\n"
    source_bundle = {
        "language": "CPP20",
        "entrypoint": "src/main.cpp",
        "files": [
            {
                "path": "src/main.cpp",
                "content": source_text,
                "content_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            }
        ],
    }
    body = {
        "session_id": "session_int2_migration",
        "draft_id": "draft_int2_migration",
        "skill_id": "skill_int2_migration",
        "content_ref": content,
        "display_name": "Migration Draft",
        "source_bundle": source_bundle,
    }
    draft = draft_resource(
        body,
        request_context,
        revision,
        created_at,
        updated_at,
        None,
    )
    session_wire = {
        "request_context": request_context,
        "session_id": body["session_id"],
        "world_id": "world_int2_migration",
        "learner_id": actor["actor_id"],
        "agent_profile_id": "agent_profile_int2_migration",
        "channel": "GAME",
        "status": "ACTIVE",
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": updated_at.isoformat().replace("+00:00", "Z"),
        "last_turn_sequence": 0,
        "content": content,
        "versions": {},
        "links": {},
    }
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO agent_sessions "
                    "(session_id,tenant_id,actor_id,command_id,world_id,status,"
                    "created_at,updated_at,session_json) VALUES "
                    "(:session_id,:tenant_id,:actor_id,:command_id,:world_id,'ACTIVE',"
                    ":created_at,:updated_at,CAST(:session_json AS jsonb))"
                ),
                {
                    "session_id": body["session_id"],
                    "tenant_id": actor["tenant_id"],
                    "actor_id": actor["actor_id"],
                    "command_id": "command_int2_migration_session",
                    "world_id": "world_int2_migration",
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "session_json": _json(session_wire),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO product_skill_drafts "
                    "(tenant_id,actor_id,session_id,draft_id,skill_id,revision,"
                    "draft_sha256,created_at,updated_at,draft_json) VALUES "
                    "(:tenant_id,:actor_id,:session_id,:draft_id,:skill_id,:revision,"
                    ":draft_sha256,:created_at,:updated_at,CAST(:draft_json AS jsonb))"
                ),
                {
                    "tenant_id": actor["tenant_id"],
                    "actor_id": actor["actor_id"],
                    "session_id": body["session_id"],
                    "draft_id": body["draft_id"],
                    "skill_id": body["skill_id"],
                    "revision": revision,
                    "draft_sha256": draft["draft_sha256"],
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "draft_json": _json(draft),
                },
            )
    finally:
        await engine.dispose()
    return draft


async def _corrupt_legacy_draft(database_url: str, kind: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            draft = (
                await connection.execute(
                    text("SELECT draft_json FROM product_skill_drafts")
                )
            ).scalar_one()
            value = dict(draft)
            if kind == "timestamp_mirror":
                value["created_at"] = "2050-01-01T00:00:00Z"
            elif kind == "session_content":
                other = {
                    "unit_id": "content_other",
                    "version": "1.0.0",
                    "content_hash": "2" * 64,
                }
                value["content_ref"] = other
                value["request_context"] = {
                    **value["request_context"],
                    "content_ref": other,
                }
                _rehash_draft(value)
            elif kind == "source_content_hash":
                value["source_bundle"]["files"][0]["content_sha256"] = "0" * 64
                _rehash_draft(value)
            elif kind == "source_case_collision":
                value["source_bundle"]["files"].append(
                    {
                        **value["source_bundle"]["files"][0],
                        "path": "SRC/MAIN.CPP",
                    }
                )
                _rehash_draft(value)
            elif kind == "unsafe_integer":
                value["display_name"] = 2**53
                value["draft_sha256"] = "0" * 64
            else:  # pragma: no cover - test parameter is closed above
                raise AssertionError(kind)
            await connection.execute(
                text(
                    "UPDATE product_skill_drafts SET draft_json=CAST(:draft AS jsonb),"
                    "draft_sha256=:draft_sha256"
                ),
                {"draft": _json(value), "draft_sha256": value["draft_sha256"]},
            )
    finally:
        await engine.dispose()


def _rehash_draft(value: dict[str, Any]) -> None:
    projection = {
        key: value[key]
        for key in (
            "session_id",
            "draft_id",
            "skill_id",
            "content_ref",
            "display_name",
            "source_bundle",
        )
    }
    value["draft_sha256"] = canonical_json_sha256(projection)


async def _migration_state(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            exists = (
                await connection.execute(
                    text("SELECT to_regclass('product_skill_draft_revisions')")
                )
            ).scalar_one()
            count = None
            revision_created_at = None
            if exists is not None:
                count = (
                    await connection.execute(
                        text("SELECT count(*) FROM product_skill_draft_revisions")
                    )
                ).scalar_one()
                revision_created_at = (
                    await connection.execute(
                        text(
                            "SELECT created_at FROM product_skill_draft_revisions "
                            "ORDER BY draft_revision_row_id LIMIT 1"
                        )
                    )
                ).scalar_one_or_none()
            draft = (
                await connection.execute(
                    text("SELECT draft_json FROM product_skill_drafts")
                )
            ).scalar_one_or_none()
        return {
            "revision": revision,
            "draft_revision_count": count,
            "draft_revision_created_at": revision_created_at,
            "draft_json": draft,
        }
    finally:
        await engine.dispose()


def _migrate(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    direction = "downgrade" if downgrade else "upgrade"
    completed = _run_alembic(database_url, direction, revision)
    assert completed.returncode == 0, completed.stderr


def _run_alembic(
    database_url: str, direction: str, revision: str
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["WALNUT_DATABASE_URL"] = database_url
    environment.pop("WALNUT_TEST_DATABASE_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "alembic", direction, revision],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


async def _create_database(admin_url: URL, database_name: str) -> None:
    _assert_scratch_name(database_name)
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(f'CREATE DATABASE "{database_name}" TEMPLATE template0')
            )
    finally:
        await engine.dispose()


async def _drop_database(admin_url: URL, database_name: str) -> None:
    _assert_scratch_name(database_name)
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database_name AND pid<>pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        await engine.dispose()


def _assert_scratch_name(value: str) -> None:
    if re.fullmatch(r"walnut_int2_[a-f0-9]{20}", value) is None:
        raise AssertionError("refusing to mutate a non-scratch PostgreSQL database")


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
