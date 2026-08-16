"""Real PostgreSQL proof for the one-shot INT1 E2E authority boundary."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    OperationContext,
    Success,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    Base,
    BuildPolicyRow,
    LaunchAuthorityRow,
    ProductContentUnitRow,
    RegistryHeadRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.session import (
    create_session_factory,
    normalize_database_url,
)
from walnut_backend.adapters.postgres.student_bootstrap import PostgresStudentBootstrapReader
from walnut_backend.api.auth import JwtAuthenticator
from walnut_backend.bootstrap import Settings
from walnut_backend.int1_e2e_authority import (
    ACTOR_ID,
    PINNED_GCC_IMAGE,
    TENANT_ID,
    Int1AuthoritySeedConfig,
    Int1AuthoritySeedError,
    build_int1_e2e_fixture,
    seed_int1_e2e_authority,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = BACKEND_ROOT.parent / "agent"
JWT_SECRET = "int1-integration-only-hs256-secret"
EXPECTED_ROWS = {
    "product_content_units": 1,
    "world_snapshots": 1,
    "learner_profiles": 1,
    "agent_profiles": 1,
    "build_policies": 1,
    "launch_authorities": 1,
    "registry_heads": 1,
}
EXPLICIT_ZERO_ROWS = {
    "agent_sessions",
    "product_skill_drafts",
    "skill_builds",
    "skill_artifacts",
    "skill_certifications",
    "skill_activations",
    "game_runs",
    "game_evidence",
    "product_agent_interactions",
}


def test_int1_seed_populates_only_canonical_preconditions_in_a_fresh_database() -> None:
    base_url = _required_test_database_url()
    database_name = f"walnut_int1_{uuid4().hex[:20]}"
    target_url = make_url(normalize_database_url(base_url)).set(database=database_name)
    asyncio.run(_create_database(make_url(normalize_database_url(base_url)), database_name))
    try:
        _migrate(target_url)
        with TemporaryDirectory(prefix="walnut-int1-authority-") as raw_root:
            artifact_root = Path(raw_root) / "artifacts"
            config = _config(target_url, artifact_root)
            result = asyncio.run(seed_int1_e2e_authority(config))
            asyncio.run(_assert_seeded_database(target_url, config, result.content_hash))
            assert result.authorization.startswith("Bearer ")
            assert result.authorization.removeprefix("Bearer ").count(".") == 2
            encoded_claims = result.authorization.removeprefix("Bearer ").split(".")[1]
            claims = json.loads(
                base64.urlsafe_b64decode(encoded_claims + "=" * (-len(encoded_claims) % 4))
            )
            assert claims["exp"] - claims["iat"] == 1800
            assert JwtAuthenticator(config.settings).authenticate(result.authorization) == ActorRef(
                TENANT_ID, ACTOR_ID, ActorType.STUDENT, ("game:player",)
            )
            assert result.authorization not in repr(result)
            assert JWT_SECRET not in repr(config)
            assert JWT_SECRET not in repr(result)
            assert JWT_SECRET not in json.dumps(result.as_json(), sort_keys=True)
            assert result.sandbox_image == PINNED_GCC_IMAGE
            assert result.registry_revision == 0
            assert artifact_root.is_dir()
            assert list(artifact_root.iterdir()) == []

            asyncio.run(_drift_agent_profile(target_url))
            with pytest.raises(Int1AuthoritySeedError, match="database is not fresh"):
                asyncio.run(seed_int1_e2e_authority(config))
            asyncio.run(_assert_row_counts(target_url))
            assert list(artifact_root.iterdir()) == []
    finally:
        asyncio.run(_drop_database(make_url(normalize_database_url(base_url)), database_name))


def _required_test_database_url() -> str:
    value = os.getenv("WALNUT_TEST_DATABASE_URL")
    if value is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL INT1 seed coverage"
        )
    return value


def _config(target_url: URL, artifact_root: Path) -> Int1AuthoritySeedConfig:
    settings = replace(
        Settings.for_test(contract_path=AGENT_ROOT),
        database_url=target_url.render_as_string(hide_password=False),
        development_auth_enabled=False,
        auth_hmac_secret=JWT_SECRET,
        auth_issuer="walnut-int1-test",
        auth_audience="walnut-int1-client",
    )
    return Int1AuthoritySeedConfig(
        settings=settings,
        artifact_root=artifact_root,
        sandbox_image=PINNED_GCC_IMAGE,
        provider_identifier="deepseek",
        model_identifier="deepseek-v4-flash",
        prompt_version="int1-prompt-v1",
        teaching_spec_version="agent-teaching-v1",
        world_rules_version="farm-rules-1",
        world_success_score=8,
    )


async def _assert_seeded_database(
    target_url: URL,
    config: Int1AuthoritySeedConfig,
    content_hash: str,
) -> None:
    sessions = create_session_factory(target_url.render_as_string(hide_password=False))
    try:
        async with sessions() as session:
            counts = {
                table.name: await session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }
            expected = {
                table.name: EXPECTED_ROWS.get(table.name, 0)
                for table in Base.metadata.sorted_tables
            }
            assert counts == expected
            assert all(counts[name] == 0 for name in EXPLICIT_ZERO_ROWS)

            content = await session.scalar(select(ProductContentUnitRow))
            profile = await session.scalar(select(AgentProfileRow))
            policy = await session.scalar(select(BuildPolicyRow))
            launch = await session.scalar(select(LaunchAuthorityRow))
            head = await session.scalar(select(RegistryHeadRow))
            world = await session.scalar(select(WorldSnapshotRow))
            assert content is not None
            assert profile is not None
            assert policy is not None
            assert launch is not None
            assert head is not None
            assert world is not None
            assert content.content_hash == content_hash
            assert content.content_json["task"]["task_id"] == "task_watering_0001"
            assert content.content_json["task"]["starter_skill"]["source_bundle"]
            assert all(
                plot["crop"] is not None and plot["crop"]["ready_to_harvest"] is True
                for plot in world.snapshot_json["state"]["plots"]
            )
            assert profile.profile_json["provider"] == config.provider_identifier
            assert profile.profile_json["model_version"] == config.model_identifier
            assert profile.profile_json["prompt_version"] == config.prompt_version
            assert profile.profile_sha256 == canonical_json_sha256(profile.profile_json)
            assert policy.policy_sha256 == canonical_json_sha256(policy.policy_json)
            assert policy.policy_json["compiler_image"] == PINNED_GCC_IMAGE
            assert policy.policy_json["parameter_schema"]["properties"]["length"] == {
                "type": "integer",
                "const": 8,
            }
            assert policy.policy_json["public_tests"]
            assert policy.policy_json["hidden_tests"]
            assert policy.policy_json["public_tests"][0]["arguments"] == ["8"]
            assert policy.policy_json["hidden_tests"][0]["arguments"] == ["8"]
            assert policy.allowed_capabilities == ["HARVEST", "WORLD_READ"]
            assert launch.authority_sha256 == build_int1_e2e_fixture(config).launch_authority_sha256
            assert head.revision == 0

        reader = PostgresStudentBootstrapReader(sessions)
        resolved = await reader.resolve(
            OperationContext(
                request_id="req_int1_seed_verify_0001",
                correlation_id="corr_int1_seed_verify_0001",
                trace_id="trace_int1_seed_verify_0001",
                requested_at=datetime.now(UTC),
                actor=ActorRef(TENANT_ID, ACTOR_ID, ActorType.STUDENT, ("game:player",)),
                content_ref=ContentRef("YAYA_FARM_001", "1.0.0", content_hash),
                command_id="cmd_int1_seed_verify_0001",
                causation_id=None,
            )
        )
        assert isinstance(resolved, Success)
        assert resolved.value.current_session_id is None
        assert resolved.value.registry_revision == 0
        assert resolved.value.active_skill is None
        assert resolved.value.world_revision == 0
    finally:
        await sessions.kw["bind"].dispose()


async def _drift_agent_profile(target_url: URL) -> None:
    sessions = create_session_factory(target_url.render_as_string(hide_password=False))
    try:
        async with sessions() as session, session.begin():
            row = await session.scalar(select(AgentProfileRow))
            assert row is not None
            row.profile_json = {**row.profile_json, "provider": "drifted-provider"}
    finally:
        await sessions.kw["bind"].dispose()


async def _assert_row_counts(target_url: URL) -> None:
    sessions = create_session_factory(target_url.render_as_string(hide_password=False))
    try:
        async with sessions() as session:
            counts = {
                table.name: await session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }
        assert counts == {
            table.name: EXPECTED_ROWS.get(table.name, 0) for table in Base.metadata.sorted_tables
        }
    finally:
        await sessions.kw["bind"].dispose()


def _migrate(target_url: URL) -> None:
    environment = dict(os.environ)
    environment["WALNUT_DATABASE_URL"] = target_url.render_as_string(hide_password=False)
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


async def _create_database(base_url: URL, database_name: str) -> None:
    _assert_scratch_database_name(database_name)
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}" TEMPLATE template0'))
    finally:
        await engine.dispose()


async def _drop_database(base_url: URL, database_name: str) -> None:
    _assert_scratch_database_name(database_name)
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
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


def _assert_scratch_database_name(value: str) -> None:
    if re.fullmatch(r"walnut_int1_[a-f0-9]{20}", value) is None:
        raise AssertionError("refusing to mutate a non-scratch PostgreSQL database")
