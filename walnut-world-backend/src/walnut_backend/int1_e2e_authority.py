"""Opt-in, fail-closed authority seed for the cross-repository INT1 E2E.

This module is deliberately separate from API and worker composition.  It may
only populate a freshly migrated database with the seven immutable authorities
that must exist before the first public student request.  It never creates a
Session, Draft, Build, Artifact, Certification, Activation, Run, Evidence, or
Interaction, and it never reads an LLM API key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_build import (
    CPP20_SAFE_V1_FLAGS,
    CPP20_SAFE_V1_PROFILE,
    canonical_source_bundle_sha256,
    validate_source_bundle,
)
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    FrozenJsonObject,
    HarvestIntent,
    RequestContext,
    WaterIntent,
    WorldSnapshot,
    canonical_json_sha256,
)
from yaya_agent_runtime import LEARNER_PROJECTION_POLICY_VERSION, REVIEW_POLICY_VERSION

from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    Base,
    BuildPolicyRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryHeadRow,
    WorldSnapshotRow,
    world_snapshot_data,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.api.auth import JwtAuthenticator
from walnut_backend.bootstrap import ContractRelease, Settings
from walnut_backend.contract_release import (
    ContractReleaseVerificationError,
    verify_agent_contract_release,
)
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules

BACKEND_ROOT = Path(__file__).resolve().parents[2]

OPT_IN_ENV = "WALNUT_INT1_E2E_SEED"
PINNED_GCC_IMAGE = "gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c"
PINNED_GCC_VERSION = "14.2.0"
TENANT_ID = "tenant_yaya"
ACTOR_ID = "student_0001"
CONTENT_UNIT_ID = "YAYA_FARM_001"
CONTENT_VERSION = "1.0.0"
TASK_ID = "task_watering_0001"
WORLD_ID = "world_watering_0001"
LEARNER_ID = ACTOR_ID
AGENT_PROFILE_ID = "agent_profile_build_e2e_0001"
AUTHORITY_ID = "authority_build_e2e_0001"
BUILD_POLICY_ID = "build_policy_e2e_0001"
SKILL_ID = "skill_process_restart_e2e_0001"
TEST_SUITE_VERSION = "build-e2e-1"
SEED_TIMESTAMP = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
# A fresh Backend database has no World stream or event rows.  The initial
# snapshot therefore starts at the only coherent checkpoint: revision/sequence
# zero.  Later public requests must use the revision returned by bootstrap.
WORLD_REVISION = 0
WORLD_EVENT_SEQUENCE = 0

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_TRUE_VALUES = {"1", "true", "yes"}
_FALSE_VALUES = {"0", "false", "no"}
_TOKEN_LIFETIME_SECONDS = 1800
_ALLOWED_TABLES = {
    "product_content_units",
    "world_snapshots",
    "learner_profiles",
    "agent_profiles",
    "build_policies",
    "launch_authorities",
    "registry_heads",
}


class Int1AuthoritySeedError(RuntimeError):
    """The requested seed would not preserve the fresh deterministic boundary."""


@dataclass(frozen=True, slots=True)
class Int1AuthoritySeedConfig:
    """Explicit identifiers plus repr-hidden production settings for the one-shot seed."""

    settings: Settings = field(repr=False)
    artifact_root: Path
    sandbox_image: str
    provider_identifier: str
    model_identifier: str
    prompt_version: str
    teaching_spec_version: str
    world_rules_version: str
    world_success_score: int
    watering: bool = False

    def __post_init__(self) -> None:
        if self.settings.development_auth_enabled:
            raise ValueError("INT1 E2E seed requires production JWT authentication")
        if any(
            value is None
            for value in (
                self.settings.auth_hmac_secret,
                self.settings.auth_issuer,
                self.settings.auth_audience,
            )
        ):
            raise ValueError("INT1 E2E seed requires the complete production JWT profile")
        if self.sandbox_image != PINNED_GCC_IMAGE:
            raise ValueError("WALNUT_SANDBOX_IMAGE differs from the pinned INT1 fixture")
        if self.world_success_score != 8:
            raise ValueError("WALNUT_WORLD_SUCCESS_SCORE must be 8 for the pinned INT1 WorldRules")
        for name, value in (
            ("WALNUT_LLM_PROVIDER", self.provider_identifier),
            ("WALNUT_LLM_MODEL", self.model_identifier),
            ("WALNUT_PROMPT_VERSION", self.prompt_version),
            ("WALNUT_TEACHING_SPEC_VERSION", self.teaching_spec_version),
            ("WALNUT_WORLD_RULES_VERSION", self.world_rules_version),
        ):
            if _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{name} must be a non-secret version identifier")
        manifest = self.settings.contract_path / "contracts" / "manifest.json"
        if not manifest.is_file():
            raise ValueError("WALNUT_CONTRACT_PATH has no current Agent contract manifest")

    @classmethod
    def from_env(cls) -> Int1AuthoritySeedConfig:
        if os.getenv(OPT_IN_ENV, "").strip().lower() not in _TRUE_VALUES:
            raise ValueError(f"set {OPT_IN_ENV}=true to opt in")
        if os.getenv("WALNUT_DEVELOPMENT_AUTH", "").strip().lower() not in _FALSE_VALUES:
            raise ValueError("set WALNUT_DEVELOPMENT_AUTH=false explicitly for this E2E seed")
        _required("WALNUT_DATABASE_URL")
        _required("WALNUT_CONTRACT_PATH")
        runtime_root = Path(_required("WALNUT_RUNTIME_ROOT")).expanduser().resolve()
        task_mode = os.getenv("WALNUT_INT1_TASK_MODE", "harvest").strip().lower()
        if task_mode not in {"harvest", "watering"}:
            raise ValueError("WALNUT_INT1_TASK_MODE must be 'harvest' or 'watering'")
        return cls(
            settings=Settings.from_env(),
            artifact_root=(runtime_root / "artifacts").resolve(),
            sandbox_image=_required("WALNUT_SANDBOX_IMAGE"),
            provider_identifier=_required_identifier("WALNUT_LLM_PROVIDER"),
            model_identifier=_required_identifier("WALNUT_LLM_MODEL"),
            prompt_version=_required_identifier("WALNUT_PROMPT_VERSION"),
            teaching_spec_version=_required_identifier("WALNUT_TEACHING_SPEC_VERSION"),
            world_rules_version=_required_identifier("WALNUT_WORLD_RULES_VERSION"),
            world_success_score=_required_integer("WALNUT_WORLD_SUCCESS_SCORE"),
            watering=task_mode == "watering",
        )


@dataclass(frozen=True, slots=True)
class Int1AuthorityFixture:
    """Canonical bytes and hashes inserted by the seed transaction."""

    content_hash: str
    content_json: dict[str, Any]
    source_bundle_sha256: str
    world_state_hash: str
    world_snapshot_json: dict[str, Any]
    learner_profile_json: dict[str, Any]
    learner_profile_sha256: str
    agent_profile_json: dict[str, Any]
    agent_profile_sha256: str
    build_policy_json: dict[str, Any]
    build_policy_sha256: str
    launch_authority_json: dict[str, Any]
    launch_authority_sha256: str


@dataclass(frozen=True, slots=True)
class Int1AuthoritySeedResult:
    """E2E handoff whose short-lived bearer credential is excluded from repr."""

    tenant_id: str
    actor_id: str
    content_unit_id: str
    content_version: str
    content_hash: str
    world_id: str
    world_revision: int
    learner_id: str
    agent_profile_id: str
    build_policy_id: str
    authority_id: str
    registry_revision: int
    source_bundle_sha256: str
    sandbox_image: str
    artifact_root: str
    authorization: str = field(repr=False)

    def as_json(self) -> dict[str, object]:
        return {
            "status": "SEEDED",
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "content": {
                "unit_id": self.content_unit_id,
                "version": self.content_version,
                "content_hash": self.content_hash,
            },
            "world_id": self.world_id,
            "world_revision": self.world_revision,
            "learner_id": self.learner_id,
            "agent_profile_id": self.agent_profile_id,
            "build_policy_id": self.build_policy_id,
            "authority_id": self.authority_id,
            "registry_revision": self.registry_revision,
            "source_bundle_sha256": self.source_bundle_sha256,
            "sandbox_image": self.sandbox_image,
            "artifact_root": self.artifact_root,
            "authorization": self.authorization,
        }


def _harvest_source() -> str:
    return """#include <iostream>
#include <stdexcept>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2) {
        return 3;
    }
    int length = 0;
    try {
        std::size_t parsed = 0;
        const std::string raw(argv[1]);
        length = std::stoi(raw, &parsed);
        if (parsed != raw.size()) {
            return 3;
        }
    } catch (const std::exception&) {
        return 3;
    }
    if (length < 0 || length > 8) {
        return 3;
    }
    std::cout << "{\\\"actions\\\":[";
    for (int index = 1; index <= length; ++index) {
        if (index != 1) {
            std::cout << ',';
        }
        std::cout
            << "{\\\"intent_id\\\":\\\"intent_harvest_000" << index
            << "\\\",\\\"action_type\\\":\\\"HARVEST\\\""
            << ",\\\"actor_entity_id\\\":\\\"avatar_0001\\\""
            << ",\\\"expected_world_revision\\\":0"
            << ",\\\"plot_id\\\":\\\"plot_000" << index
            << "\\\"}";
    }
    std::cout << "]}";
    return 0;
}
"""


def build_int1_e2e_fixture(config: Int1AuthoritySeedConfig) -> Int1AuthorityFixture:
    """Build the exact Agent-derived Task/Build authority without touching storage."""

    is_watering = config.watering
    if is_watering:
        task_id = "task_crop_watering_0001"
        world_id = "world_crop_watering_0001"
        build_policy_id = "build_policy_watering_0001"
        skill_id = "skill_crop_watering_0001"
        authority_id = "authority_crop_watering_0001"
        agent_profile_id = "agent_profile_crop_watering_0001"
    else:
        task_id = TASK_ID
        world_id = WORLD_ID
        build_policy_id = BUILD_POLICY_ID
        skill_id = SKILL_ID
        authority_id = AUTHORITY_ID
        agent_profile_id = AGENT_PROFILE_ID
    source = _watering_source() if is_watering else _harvest_source()
    source_bundle: dict[str, object] = {
        "language": "CPP20",
        "entrypoint": "main.cpp",
        "files": [
            {
                "path": "main.cpp",
                "content": source,
                "content_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            }
        ],
    }
    validate_source_bundle(source_bundle)
    source_bundle_sha256 = canonical_source_bundle_sha256(source_bundle)
    allowed_capabilities = (
        ["WATER", "WORLD_READ"] if is_watering else ["HARVEST", "WORLD_READ"]
    )
    if is_watering:
        task: dict[str, object] = {
            "task_id": task_id,
            "name": "Water every thirsty plot",
            "goal": "Use one loop to water every plot that needs watering.",
            "instructions": [
                "Compile the starter as C++20.",
                "Read the plot count from the single process argument; stdin is closed.",
                "For each plot compute the gap between its target and current moisture.",
                "Emit a WATER action for each plot whose gap is positive: 2 units when the gap is at least 30, otherwise 1 unit.",
            ],
            "knowledge_points": ["for_loop", "conditionals"],
            "allowed_capabilities": allowed_capabilities,
            "starter_skill": {
                "skill_id": skill_id,
                "display_name": "Crop Watering Skill",
                "source_bundle": source_bundle,
                "compiler_profile": CPP20_SAFE_V1_PROFILE,
                "test_suite_version": TEST_SUITE_VERSION,
            },
            "hint_policy": {
                "max_level": 4,
                "levels": [
                    {"level": level, "instruction": f"Hint authority level {level}."}
                    for level in range(5)
                ],
            },
            "story": {
                "opening": "The mixed crop field is thirsty, but not every plot needs the same amount of water.",
                "success": "Every plot that needed water received exactly the right amount.",
            },
        }
    else:
        task = {
            "task_id": task_id,
            "name": "Harvest every plot",
            "goal": "Use one loop to harvest all eight mature plots.",
            "instructions": [
                "Compile the starter as C++20.",
                "Read the plot count from the single process argument; stdin is closed.",
                "Emit one strict HARVEST action intent for each plot as compact JSON.",
            ],
            "knowledge_points": ["for_loop", "sequence"],
            "allowed_capabilities": allowed_capabilities,
            "starter_skill": {
                "skill_id": skill_id,
                "display_name": "Process Restart Docker Skill",
                "source_bundle": source_bundle,
                "compiler_profile": CPP20_SAFE_V1_PROFILE,
                "test_suite_version": TEST_SUITE_VERSION,
            },
            "hint_policy": {
                "max_level": 4,
                "levels": [
                    {"level": level, "instruction": f"Hint authority level {level}."}
                    for level in range(5)
                ],
            },
            "story": {
                "opening": "The mature crops must be gathered before sunset.",
                "success": "Every plot has been harvested.",
            },
        }
    content_hash_basis = {
        "schema_version": "1.0.0",
        "unit_id": CONTENT_UNIT_ID,
        "version": CONTENT_VERSION,
        "audiences": ["LEARNER"],
        "task": task,
    }
    content_hash = canonical_json_sha256(content_hash_basis)
    content_ref = {
        "unit_id": CONTENT_UNIT_ID,
        "version": CONTENT_VERSION,
        "content_hash": content_hash,
    }
    content_json: dict[str, Any] = {
        "content_ref": content_ref,
        "status": "PUBLISHED",
        "unit_type": "TASK",
        "audiences": ["LEARNER"],
        "task": task,
        "published_at": _timestamp(SEED_TIMESTAMP),
        "links": {
            "self": (
                f"/product-experience/v1/content-units/{CONTENT_UNIT_ID}/versions/"
                f"{CONTENT_VERSION}?content_hash={content_hash}"
            )
        },
    }
    contract_errors = ContractRelease(config.settings).validate(
        "contracts/schemas/product-experience/content-unit.schema.json",
        content_json,
    )
    if contract_errors:
        raise Int1AuthoritySeedError("canonical ProductContentUnit violates the current contract")

    actor = ActorRef(TENANT_ID, ACTOR_ID, ActorType.STUDENT, ("game:player",))
    request_context = RequestContext(
        request_id="req_runtime_0001",
        correlation_id="corr_runtime_0001",
        trace_id="trace_runtime_0001",
        requested_at=SEED_TIMESTAMP,
        actor=actor,
        content_ref=ContentRef(CONTENT_UNIT_ID, CONTENT_VERSION, content_hash),
    )
    world_state = _world_state()
    if is_watering:
        _assert_watering_world_closure(world_state, config.world_success_score)
    else:
        _assert_harvest_world_closure(world_state, config.world_success_score)
    world_state_hash = canonical_json_sha256(world_state)
    world_snapshot = WorldSnapshot(
        request_context=request_context,
        world_id=world_id,
        revision=WORLD_REVISION,
        last_event_sequence=WORLD_EVENT_SEQUENCE,
        state_hash=world_state_hash,
        generated_at=SEED_TIMESTAMP,
        world_rules_version=config.world_rules_version,
        state=world_state,
    )
    world_snapshot_json = world_snapshot_data(world_snapshot)
    # The level's starting World, recorded once so every later Run is scored as
    # an independent attempt instead of continuing from the previous result.
    # Without it a correct program passes exactly once, and one overshooting Run
    # makes the level permanently unreachable.
    world_snapshot_json["baseline_state"] = json.loads(json.dumps(world_state))

    learner_profile_json: dict[str, Any] = {
        "schema_version": "1.0.0",
        "learner_id": LEARNER_ID,
        "actor_id": ACTOR_ID,
        "content": content_ref,
        "locale": "en-US",
        "revision": 0,
        "projected_through_sequence": 0,
        "model_version": LEARNER_PROJECTION_POLICY_VERSION,
        "review_policy_version": REVIEW_POLICY_VERSION,
        "competencies": {},
        "evidence_refs": [],
        "updated_at": _timestamp(SEED_TIMESTAMP),
    }
    learner_profile_sha256 = canonical_json_sha256(learner_profile_json)
    agent_profile_json: dict[str, Any] = {
        "schema_version": "1.0.0",
        "agent_profile_id": agent_profile_id,
        "actor_id": ACTOR_ID,
        "content": content_ref,
        "role": "farmer_build_tutor",
        "revision": 1,
        "provider": config.provider_identifier,
        "model_version": config.model_identifier,
        "prompt_version": config.prompt_version,
    }
    agent_profile_sha256 = canonical_json_sha256(agent_profile_json)

    expected_stdout = None if is_watering else _harvest_stdout(8)
    build_policy_json: dict[str, Any] = {
        "schema_version": "1.0.0",
        "compiler_image": config.sandbox_image,
        "compiler_profile": CPP20_SAFE_V1_PROFILE,
        "compiler_version": PINNED_GCC_VERSION,
        "test_suite_version": TEST_SUITE_VERSION,
        "compile_flags": list(CPP20_SAFE_V1_FLAGS),
        "public_tests": [
            _test_case(
                "public_exact_io_0001",
                "PUBLIC",
                "8",
                b"",
                expected_stdout,
            )
        ],
        "hidden_tests": [
            _test_case(
                "hidden_exact_io_0001",
                "HIDDEN",
                "8",
                b"",
                expected_stdout,
            )
        ],
        "parameter_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["length"],
            "properties": {"length": {"type": "integer", "const": 8}},
        },
        "limits": {
            "compile_wall_ms": 120_000,
            "test_wall_ms": 15_000,
            "memory_bytes": 536_870_912,
            "max_processes": 64,
            "cpu_millis": 1_000,
            "tmpfs_bytes": 67_108_864,
            "max_output_bytes": 65_536,
            "max_artifact_bytes": 16_777_216,
        },
    }
    build_policy_sha256 = canonical_json_sha256(build_policy_json)
    launch_authority_json: dict[str, Any] = {
        "schema_version": "1.0.0",
        "authority_id": authority_id,
        "actor_id": ACTOR_ID,
        "content": content_ref,
        "world_id": world_id,
        "learner_id": LEARNER_ID,
        "agent_profile_id": agent_profile_id,
        "build_policy_id": build_policy_id,
        "channel": "GAME",
        "teaching_spec_version": config.teaching_spec_version,
        "active": True,
    }
    launch_authority_sha256 = canonical_json_sha256(launch_authority_json)
    return Int1AuthorityFixture(
        content_hash=content_hash,
        content_json=content_json,
        source_bundle_sha256=source_bundle_sha256,
        world_state_hash=world_state_hash,
        world_snapshot_json=world_snapshot_json,
        learner_profile_json=learner_profile_json,
        learner_profile_sha256=learner_profile_sha256,
        agent_profile_json=agent_profile_json,
        agent_profile_sha256=agent_profile_sha256,
        build_policy_json=build_policy_json,
        build_policy_sha256=build_policy_sha256,
        launch_authority_json=launch_authority_json,
        launch_authority_sha256=launch_authority_sha256,
    )


async def seed_int1_e2e_authority(
    config: Int1AuthoritySeedConfig,
) -> Int1AuthoritySeedResult:
    """Atomically insert and verify only the seven allowed precondition rows."""

    _verify_contract_release(config.settings)
    fixture = build_int1_e2e_fixture(config)
    authorization = _issue_student_authorization(config.settings)
    _assert_empty_artifact_root(config.artifact_root, create=True)
    sessions = create_session_factory(config.settings.database_url)
    try:
        async with sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('walnut:int1-e2e-authority-seed', 0))"
                )
            )
            await _assert_migrated_head(session)
            before = await _table_counts(session)
            occupied = {name: count for name, count in before.items() if count != 0}
            if occupied:
                raise Int1AuthoritySeedError(
                    "database is not fresh; existing or drifted business rows were found"
                )
            _add_preconditions(session, config, fixture)
            await session.flush()
            await _verify_seeded_transaction(session, config, fixture)
            _assert_empty_artifact_root(config.artifact_root)
    except SQLAlchemyError as error:
        raise Int1AuthoritySeedError(
            "database migration, freshness, or seed transaction failed"
        ) from error
    finally:
        await sessions.kw["bind"].dispose()
    _assert_empty_artifact_root(config.artifact_root)
    launch_authority = fixture.launch_authority_json
    return Int1AuthoritySeedResult(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        content_unit_id=CONTENT_UNIT_ID,
        content_version=CONTENT_VERSION,
        content_hash=fixture.content_hash,
        world_id=str(launch_authority["world_id"]),
        world_revision=WORLD_REVISION,
        learner_id=LEARNER_ID,
        agent_profile_id=str(launch_authority["agent_profile_id"]),
        build_policy_id=str(launch_authority["build_policy_id"]),
        authority_id=str(launch_authority["authority_id"]),
        registry_revision=0,
        source_bundle_sha256=fixture.source_bundle_sha256,
        sandbox_image=config.sandbox_image,
        artifact_root=str(config.artifact_root),
        authorization=authorization,
    )


def _add_preconditions(
    session: AsyncSession,
    config: Int1AuthoritySeedConfig,
    fixture: Int1AuthorityFixture,
) -> None:
    digest = config.sandbox_image.rsplit("@", 1)[1]
    launch = fixture.launch_authority_json
    world_id = str(launch["world_id"])
    agent_profile_id = str(launch["agent_profile_id"])
    build_policy_id = str(launch["build_policy_id"])
    allowed_capabilities = (
        ["WATER", "WORLD_READ"] if config.watering else ["HARVEST", "WORLD_READ"]
    )
    session.add_all(
        [
            ProductContentUnitRow(
                tenant_id=TENANT_ID,
                unit_id=CONTENT_UNIT_ID,
                version=CONTENT_VERSION,
                content_hash=fixture.content_hash,
                audiences=["LEARNER"],
                published_at=SEED_TIMESTAMP,
                content_json=fixture.content_json,
            ),
            WorldSnapshotRow(
                tenant_id=TENANT_ID,
                world_id=world_id,
                actor_id=ACTOR_ID,
                content_hash=fixture.content_hash,
                revision=WORLD_REVISION,
                last_event_sequence=WORLD_EVENT_SEQUENCE,
                state_hash=fixture.world_state_hash,
                generated_at=SEED_TIMESTAMP,
                snapshot_json=fixture.world_snapshot_json,
            ),
            LearnerProfileRow(
                tenant_id=TENANT_ID,
                learner_id=LEARNER_ID,
                actor_id=ACTOR_ID,
                content_hash=fixture.content_hash,
                profile_sha256=fixture.learner_profile_sha256,
                profile_json=fixture.learner_profile_json,
                created_at=SEED_TIMESTAMP,
                updated_at=SEED_TIMESTAMP,
            ),
            AgentProfileRow(
                tenant_id=TENANT_ID,
                agent_profile_id=agent_profile_id,
                actor_id=ACTOR_ID,
                content_hash=fixture.content_hash,
                profile_sha256=fixture.agent_profile_sha256,
                profile_json=fixture.agent_profile_json,
                created_at=SEED_TIMESTAMP,
            ),
            BuildPolicyRow(
                tenant_id=TENANT_ID,
                build_policy_id=build_policy_id,
                actor_id=ACTOR_ID,
                content_hash=fixture.content_hash,
                compiler_profile=CPP20_SAFE_V1_PROFILE,
                compiler_version=PINNED_GCC_VERSION,
                sandbox_image_digest=digest,
                test_suite_version=TEST_SUITE_VERSION,
                allowed_capabilities=allowed_capabilities,
                max_source_files=32,
                max_source_bytes=1_048_576,
                policy_json=fixture.build_policy_json,
                policy_sha256=fixture.build_policy_sha256,
                active=True,
                created_at=SEED_TIMESTAMP,
            ),
        ]
    )
    # The ORM has no relationships for these immutable authority tables; use
    # explicit flush boundaries so PostgreSQL foreign keys see each parent.


async def _verify_seeded_transaction(
    session: AsyncSession,
    config: Int1AuthoritySeedConfig,
    fixture: Int1AuthorityFixture,
) -> None:
    # The first flush persisted the five independent parents.  Add the launch
    # authority and registry head in FK order inside the same transaction.
    launch = fixture.launch_authority_json
    session.add(
        LaunchAuthorityRow(
            tenant_id=TENANT_ID,
            authority_id=str(launch["authority_id"]),
            actor_id=ACTOR_ID,
            content_unit_id=CONTENT_UNIT_ID,
            content_version=CONTENT_VERSION,
            content_hash=fixture.content_hash,
            world_id=str(launch["world_id"]),
            learner_id=LEARNER_ID,
            agent_profile_id=str(launch["agent_profile_id"]),
            build_policy_id=str(launch["build_policy_id"]),
            channel="GAME",
            teaching_spec_version=config.teaching_spec_version,
            authority_sha256=fixture.launch_authority_sha256,
            active=True,
            created_at=SEED_TIMESTAMP,
        )
    )
    await session.flush()
    session.add(
        RegistryHeadRow(
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            content_hash=fixture.content_hash,
            world_id=str(launch["world_id"]),
            agent_profile_id=str(launch["agent_profile_id"]),
            authority_id=str(launch["authority_id"]),
            revision=0,
            updated_at=SEED_TIMESTAMP,
        )
    )
    await session.flush()

    counts = await _table_counts(session)
    expected = {name: (1 if name in _ALLOWED_TABLES else 0) for name in counts}
    if counts != expected:
        raise Int1AuthoritySeedError("seed transaction produced rows outside the allowed boundary")

    content = await session.scalar(select(ProductContentUnitRow))
    world = await session.scalar(select(WorldSnapshotRow))
    learner = await session.scalar(select(LearnerProfileRow))
    profile = await session.scalar(select(AgentProfileRow))
    policy = await session.scalar(select(BuildPolicyRow))
    launch = await session.scalar(select(LaunchAuthorityRow))
    head = await session.scalar(select(RegistryHeadRow))
    if any(row is None for row in (content, world, learner, profile, policy, launch, head)):
        raise Int1AuthoritySeedError("one or more required authority rows disappeared")
    assert content is not None
    assert world is not None
    assert learner is not None
    assert profile is not None
    assert policy is not None
    assert launch is not None
    assert head is not None
    if (
        content.content_json != fixture.content_json
        or world.snapshot_json != fixture.world_snapshot_json
        or world.state_hash != canonical_json_sha256(world.snapshot_json["state"])
        or learner.profile_json != fixture.learner_profile_json
        or learner.profile_sha256 != canonical_json_sha256(learner.profile_json)
        or profile.profile_json != fixture.agent_profile_json
        or profile.profile_sha256 != canonical_json_sha256(profile.profile_json)
        or policy.policy_json != fixture.build_policy_json
        or policy.policy_sha256 != canonical_json_sha256(policy.policy_json)
        or launch.authority_sha256 != canonical_json_sha256(fixture.launch_authority_json)
        or head.revision != 0
    ):
        raise Int1AuthoritySeedError("canonical authority bytes or hashes drifted before commit")


async def _assert_migrated_head(session: AsyncSession) -> None:
    alembic = AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))
    alembic.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    expected = ScriptDirectory.from_config(alembic).get_current_head()
    if expected is None:
        raise Int1AuthoritySeedError("repository has no single Alembic head")
    try:
        revisions = tuple(await session.scalars(text("SELECT version_num FROM alembic_version")))
    except SQLAlchemyError as error:
        raise Int1AuthoritySeedError("database has no Alembic migration authority") from error
    if revisions != (expected,):
        raise Int1AuthoritySeedError("database is not migrated to the repository Alembic head")


async def _table_counts(session: AsyncSession) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        count = await session.scalar(select(func.count()).select_from(table))
        if not isinstance(count, int):
            raise Int1AuthoritySeedError("PostgreSQL returned an invalid table count")
        result[table.name] = count
    return result


def _world_state() -> FrozenJsonObject:
    return cast(
        FrozenJsonObject,
        {
            "clock": {"day": 1, "minute_of_day": 480, "tick": 10},
            "avatar": {
                "entity_id": "avatar_0001",
                "position": {"x": 0, "y": 0},
                "energy": 100,
            },
            "inventory": [],
            "plots": [
                {
                    "plot_id": f"plot_{index:04d}",
                    "position": {"x": index, "y": 0},
                    "soil_state": "TILLED",
                    "hydration": 0,
                    "crop": {
                        "crop_type": "tomato",
                        "growth_stage": 2,
                        "planted_at_tick": 10,
                        "ready_to_harvest": True,
                    },
                    "last_updated_event_sequence": 0,
                }
                for index in range(1, 9)
            ],
            "agents": [],
        },
    )


def _harvest_stdout(length: int) -> bytes:
    actions = [
        {
            "intent_id": f"intent_harvest_{index:04d}",
            "action_type": "HARVEST",
            "actor_entity_id": "avatar_0001",
            "expected_world_revision": WORLD_REVISION,
            "plot_id": f"plot_{index:04d}",
        }
        for index in range(1, length + 1)
    ]
    return json.dumps({"actions": actions}, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )


# The crop watering fixture: 8 plots with moisture and target arrays; a plot is
# watered 2 units when the gap is at least 30, 1 unit when the gap is positive,
# and skipped otherwise.  These match the frontend crop level EXPECTED_UNITS.
WATERING_MOISTURE = (20, 65, 45, 90, 60, 35, 55, 50)
WATERING_TARGET = (60, 70, 50, 65, 60, 70, 50, 65)
WATERING_EXPECTED_UNITS = (2, 1, 1, 0, 0, 2, 0, 1)


def _watering_source() -> str:
    moisture = ", ".join(str(value) for value in WATERING_MOISTURE)
    target = ", ".join(str(value) for value in WATERING_TARGET)
    return f"""#include <iostream>
using namespace std;

int main() {{
    int moisture[8] = {{{moisture}}};
    int target[8]   = {{{target}}};

    for (int i = 0; i < 8; i++) {{
        // 第一步：将两个标记替换为目标数组名和当前数组名
        int gap = /*目标*/[i] - /*当前*/[i];

        // 第二步：将边界和份数标记替换为正确数字
        if (gap >= /*边界*/) {{
            cout << "WATER " << i << " /*份数*/\\n";
        }} else if (gap > /*边界*/) {{
            cout << "WATER " << i << " /*份数*/\\n";
        }}

        // 提示：gap <= 0 时不输出 WATER，喷头保持关闭
        // 真实运行出错后，可继续修改整个循环体
    }}
    return 0;
}}
"""


def _watering_stdout(length: int) -> bytes:
    lines = [
        f"WATER {index} {amount}\n"
        for index in range(length)
        if (amount := _expected_units(index)) > 0
    ]
    return "".join(lines).encode("utf-8")


def _expected_units(index: int) -> int:
    gap = WATERING_TARGET[index] - WATERING_MOISTURE[index]
    if gap >= 30:
        return 2
    if gap > 0:
        return 1
    return 0


def _assert_harvest_world_closure(world_state: FrozenJsonObject, world_success_score: int) -> None:
    intents = tuple(
        HarvestIntent(
            f"intent_harvest_{index:04d}",
            "avatar_0001",
            WORLD_REVISION,
            f"plot_{index:04d}",
        )
        for index in range(1, 9)
    )
    rules = WorldRules(
        content_version=CONTENT_VERSION,
        max_actions=8,
        min_x=0,
        max_x=31,
        min_y=0,
        max_y=31,
        harvest_growth_stage=2,
        success_score=world_success_score,
    )
    engine = WorldEngine()
    incomplete = engine.apply(world_state, intents[:-1], rules)
    transition = engine.apply(world_state, intents, rules)
    expected_ids = tuple(intent.intent_id for intent in intents)
    if (
        incomplete.score != 7
        or incomplete.success
        or incomplete.applied_intent_ids != expected_ids[:-1]
        or transition.score != 8
        or not transition.success
        or transition.applied_intent_ids != expected_ids
    ):
        raise Int1AuthoritySeedError(
            "canonical HARVEST fixture does not satisfy the pinned 7/8 WorldRules boundary"
        )


def _assert_watering_world_closure(
    world_state: FrozenJsonObject, world_success_score: int
) -> None:
    rules = WorldRules(
        content_version=CONTENT_VERSION,
        max_actions=8,
        min_x=0,
        max_x=31,
        min_y=0,
        max_y=31,
        harvest_growth_stage=2,
        success_score=world_success_score,
        watering_expected_units=WATERING_EXPECTED_UNITS,
    )
    engine = WorldEngine()
    intents = tuple(
        WaterIntent(
            f"intent_water_{index + 1:04d}",
            "avatar_0001",
            WORLD_REVISION,
            f"plot_{index + 1:04d}",
            amount,
        )
        for index, amount in enumerate(WATERING_EXPECTED_UNITS)
        if amount > 0
    )
    incomplete = engine.apply(world_state, intents[:-1], rules)
    transition = engine.apply(world_state, intents, rules)
    if (
        incomplete.score != 7
        or incomplete.success
        or transition.score != 8
        or not transition.success
    ):
        raise Int1AuthoritySeedError(
            "canonical WATERING fixture does not satisfy the pinned 7/8 WorldRules boundary"
        )


def _verify_contract_release(settings: Settings) -> None:
    release = settings.contract_release_path or BACKEND_ROOT / "contract-release.json"
    try:
        verify_agent_contract_release(settings.contract_path, release)
    except ContractReleaseVerificationError as error:
        raise Int1AuthoritySeedError(
            "backend-pinned Agent contract release verification failed"
        ) from error


def _issue_student_authorization(settings: Settings) -> str:
    secret = settings.auth_hmac_secret
    issuer = settings.auth_issuer
    audience = settings.auth_audience
    if secret is None or issuer is None or audience is None:
        raise Int1AuthoritySeedError("production JWT settings became incomplete")
    if settings.auth_maximum_lifetime_seconds < _TOKEN_LIFETIME_SECONDS:
        raise Int1AuthoritySeedError(
            "production maximum JWT lifetime is shorter than the INT1 E2E lifetime"
        )
    now = datetime.now(UTC)
    issued_at = int(now.timestamp())
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": ACTOR_ID,
        "tenant_id": TENANT_ID,
        "actor_id": ACTOR_ID,
        "actor_type": ActorType.STUDENT.value,
        "roles": ["game:player"],
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + _TOKEN_LIFETIME_SECONDS,
    }
    header: dict[str, object] = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _base64url_json(header)
    encoded_claims = _base64url_json(claims)
    signing_input = f"{encoded_header}.{encoded_claims}"
    signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    token = f"{signing_input}.{_base64url(signature)}"
    authorization = f"Bearer {token}"
    expected_actor = ActorRef(TENANT_ID, ACTOR_ID, ActorType.STUDENT, ("game:player",))
    if JwtAuthenticator(settings).authenticate(authorization, now=now) != expected_actor:
        raise Int1AuthoritySeedError("issued production JWT failed identity closure")
    return authorization


def _base64url_json(value: dict[str, object]) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    return _base64url(raw)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _test_case(
    test_case_id: str,
    visibility: str,
    argument: str,
    stdin: bytes,
    expected_stdout: bytes | None,
) -> dict[str, object]:
    return {
        "test_case_id": test_case_id,
        "visibility": visibility,
        "arguments": [argument],
        "stdin_base64": base64.b64encode(stdin).decode("ascii"),
        "expected_stdout_sha256": (
            hashlib.sha256(expected_stdout).hexdigest()
            if expected_stdout is not None
            else None
        ),
    }


def _assert_empty_artifact_root(path: Path, *, create: bool = False) -> None:
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=False)
    if path.is_symlink() or not path.is_dir():
        raise Int1AuthoritySeedError("artifact root must be a real directory")
    if any(path.iterdir()):
        raise Int1AuthoritySeedError("artifact root must be empty before the first Build")


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _required_identifier(name: str) -> str:
    value = _required(name)
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a non-secret version identifier")
    return value


def _required_integer(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def main() -> None:
    try:
        result = asyncio.run(seed_int1_e2e_authority(Int1AuthoritySeedConfig.from_env()))
    except (Int1AuthoritySeedError, ValueError) as error:
        print(f"INT1_E2E_AUTHORITY_SEED_REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as error:  # pragma: no cover - defensive secret-safe CLI boundary
        print(
            f"INT1_E2E_AUTHORITY_SEED_REFUSED: unexpected {type(error).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result.as_json(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "ACTOR_ID",
    "AGENT_PROFILE_ID",
    "AUTHORITY_ID",
    "BUILD_POLICY_ID",
    "CONTENT_UNIT_ID",
    "CONTENT_VERSION",
    "Int1AuthorityFixture",
    "Int1AuthoritySeedConfig",
    "Int1AuthoritySeedError",
    "Int1AuthoritySeedResult",
    "PINNED_GCC_IMAGE",
    "TASK_ID",
    "TENANT_ID",
    "WORLD_ID",
    "build_int1_e2e_fixture",
    "seed_int1_e2e_authority",
]
