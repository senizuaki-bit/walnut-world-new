"""Read-only proof for reusing the current persistent watering authority."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase
from yaya_agent_build import CPP20_SAFE_V1_FLAGS, canonical_source_bundle_sha256
from yaya_agent_contracts import canonical_json_sha256

from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    BuildPolicyRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryHeadRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.int1_e2e_authority import (
    ACTOR_ID,
    CONTENT_UNIT_ID,
    CONTENT_VERSION,
    LEARNER_ID,
    TENANT_ID,
    Int1AuthorityFixture,
    Int1AuthoritySeedConfig,
    build_int1_e2e_fixture,
)

CURRENT_WATERING_CONTENT_HASH = (
    "05335ff1ba5540a6f424b58e7f3664cc1884b356a59297c4c0b0980de50445b4"
)
CURRENT_WATERING_SOURCE_BUNDLE_SHA256 = (
    "8796181f019f64cd8bad67d663ff67f9e45d218fc67955a8b3c4ce99da398267"
)
CURRENT_WATERING_BUILD_POLICY_SHA256 = (
    "6089100b44ffde157d85d173e8c4cb191434cf85e74b21e65b9882bfda8ca4fa"
)
CURRENT_WATERING_LAUNCH_AUTHORITY_SHA256 = (
    "3074492e0175f2bc410be8892fd3ace3ef8dfe6a99953bc8c709de96f2d2d8b0"
)


class PersistentPlayAuthorityError(RuntimeError):
    """A stable, secret-free reason why existing authority cannot be reused."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PersistentPlayAuthoritySummary:
    content_hash: str
    source_bundle_sha256: str
    build_policy_sha256: str
    launch_authority_sha256: str
    compile_flags: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "status": "CURRENT_WATERING_AUTHORITY_VALID",
            "authority_rows": 7,
            "content_hash": self.content_hash,
            "source_bundle_sha256": self.source_bundle_sha256,
            "build_policy_sha256": self.build_policy_sha256,
            "launch_authority_sha256": self.launch_authority_sha256,
            "compile_flags": list(self.compile_flags),
            "read_only": True,
        }


async def verify_persistent_watering_authority(
    config: Int1AuthoritySeedConfig,
) -> PersistentPlayAuthoritySummary:
    """Prove all seven current watering authorities without trusting seed output."""

    if not config.watering:
        raise PersistentPlayAuthorityError("CONFIG_NOT_WATERING")
    fixture = build_int1_e2e_fixture(config)
    if (
        fixture.content_hash != CURRENT_WATERING_CONTENT_HASH
        or fixture.source_bundle_sha256 != CURRENT_WATERING_SOURCE_BUNDLE_SHA256
        or fixture.build_policy_sha256 != CURRENT_WATERING_BUILD_POLICY_SHA256
        or fixture.launch_authority_sha256 != CURRENT_WATERING_LAUNCH_AUTHORITY_SHA256
        or fixture.build_policy_json.get("compile_flags") != list(CPP20_SAFE_V1_FLAGS)
    ):
        raise PersistentPlayAuthorityError("CURRENT_WATERING_FIXTURE_DRIFT")

    sessions = create_session_factory(config.settings.database_url)
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            rows = {
                "content": await _exactly_one(session, ProductContentUnitRow),
                "world": await _exactly_one(session, WorldSnapshotRow),
                "learner": await _exactly_one(session, LearnerProfileRow),
                "profile": await _exactly_one(session, AgentProfileRow),
                "policy": await _exactly_one(session, BuildPolicyRow),
                "launch": await _exactly_one(session, LaunchAuthorityRow),
                "head": await _exactly_one(session, RegistryHeadRow),
            }
            _validate_rows(config, fixture, rows)
    finally:
        await sessions.kw["bind"].dispose()

    return PersistentPlayAuthoritySummary(
        content_hash=fixture.content_hash,
        source_bundle_sha256=fixture.source_bundle_sha256,
        build_policy_sha256=fixture.build_policy_sha256,
        launch_authority_sha256=fixture.launch_authority_sha256,
        compile_flags=tuple(CPP20_SAFE_V1_FLAGS),
    )


async def _exactly_one[ModelBase: DeclarativeBase](
    session: AsyncSession,
    model: type[ModelBase],
) -> ModelBase:
    table_name = model.__table__.name
    count = await session.scalar(select(func.count()).select_from(model))
    if count != 1:
        code_name = table_name.upper().replace("-", "_")
        raise PersistentPlayAuthorityError(f"{code_name}_ROW_COUNT_INVALID")
    row = await session.scalar(select(model))
    if row is None:
        raise PersistentPlayAuthorityError("AUTHORITY_ROW_DISAPPEARED")
    return row


def _validate_rows(
    config: Int1AuthoritySeedConfig,
    fixture: Int1AuthorityFixture,
    values: dict[str, Any],
) -> None:
    content: ProductContentUnitRow = values["content"]
    world: WorldSnapshotRow = values["world"]
    learner: LearnerProfileRow = values["learner"]
    profile: AgentProfileRow = values["profile"]
    policy: BuildPolicyRow = values["policy"]
    launch: LaunchAuthorityRow = values["launch"]
    head: RegistryHeadRow = values["head"]

    if (
        content.tenant_id != TENANT_ID
        or content.unit_id != CONTENT_UNIT_ID
        or content.version != CONTENT_VERSION
        or content.content_hash != fixture.content_hash
        or content.content_json != fixture.content_json
    ):
        raise PersistentPlayAuthorityError("CONTENT_AUTHORITY_MISMATCH")
    try:
        source_bundle = content.content_json["task"]["starter_skill"]["source_bundle"]
        source_hash = canonical_source_bundle_sha256(source_bundle)
    except (KeyError, TypeError, ValueError):
        raise PersistentPlayAuthorityError("SOURCE_BUNDLE_INVALID") from None
    if source_hash != CURRENT_WATERING_SOURCE_BUNDLE_SHA256:
        raise PersistentPlayAuthorityError("SOURCE_BUNDLE_HASH_MISMATCH")

    launch_json = fixture.launch_authority_json
    expected_world_id = str(launch_json["world_id"])
    if (
        world.tenant_id != TENANT_ID
        or world.world_id != expected_world_id
        or world.actor_id != ACTOR_ID
        or world.content_hash != fixture.content_hash
    ):
        raise PersistentPlayAuthorityError("WORLD_AUTHORITY_MISMATCH")
    try:
        if canonical_json_sha256(world.snapshot_json["state"]) != world.state_hash:
            raise PersistentPlayAuthorityError("WORLD_STATE_HASH_MISMATCH")
    except (KeyError, TypeError, ValueError):
        raise PersistentPlayAuthorityError("WORLD_STATE_INVALID") from None

    if (
        learner.tenant_id != TENANT_ID
        or learner.learner_id != LEARNER_ID
        or learner.actor_id != ACTOR_ID
        or learner.content_hash != fixture.content_hash
    ):
        raise PersistentPlayAuthorityError("LEARNER_AUTHORITY_MISMATCH")
    if canonical_json_sha256(learner.profile_json) != learner.profile_sha256:
        raise PersistentPlayAuthorityError("LEARNER_PROFILE_HASH_MISMATCH")

    if (
        profile.tenant_id != TENANT_ID
        or profile.agent_profile_id != str(launch_json["agent_profile_id"])
        or profile.actor_id != ACTOR_ID
        or profile.content_hash != fixture.content_hash
        or profile.profile_json != fixture.agent_profile_json
        or profile.profile_sha256 != fixture.agent_profile_sha256
    ):
        raise PersistentPlayAuthorityError("AGENT_PROFILE_AUTHORITY_MISMATCH")

    flags = policy.policy_json.get("compile_flags")
    if flags != list(CPP20_SAFE_V1_FLAGS):
        raise PersistentPlayAuthorityError("BUILD_POLICY_FLAGS_MISMATCH")
    if (
        policy.tenant_id != TENANT_ID
        or policy.build_policy_id != str(launch_json["build_policy_id"])
        or policy.actor_id != ACTOR_ID
        or policy.content_hash != fixture.content_hash
        or policy.policy_json != fixture.build_policy_json
        or policy.policy_sha256 != CURRENT_WATERING_BUILD_POLICY_SHA256
        or canonical_json_sha256(policy.policy_json) != policy.policy_sha256
        or not policy.active
    ):
        raise PersistentPlayAuthorityError("BUILD_POLICY_AUTHORITY_MISMATCH")

    if (
        launch.tenant_id != TENANT_ID
        or launch.authority_id != str(launch_json["authority_id"])
        or launch.actor_id != ACTOR_ID
        or launch.content_unit_id != CONTENT_UNIT_ID
        or launch.content_version != CONTENT_VERSION
        or launch.content_hash != fixture.content_hash
        or launch.world_id != expected_world_id
        or launch.learner_id != LEARNER_ID
        or launch.agent_profile_id != str(launch_json["agent_profile_id"])
        or launch.build_policy_id != str(launch_json["build_policy_id"])
        or launch.channel != "GAME"
        or launch.teaching_spec_version != config.teaching_spec_version
        or launch.authority_sha256 != CURRENT_WATERING_LAUNCH_AUTHORITY_SHA256
        or canonical_json_sha256(launch_json) != launch.authority_sha256
        or not launch.active
    ):
        raise PersistentPlayAuthorityError("LAUNCH_AUTHORITY_MISMATCH")

    if (
        head.tenant_id != TENANT_ID
        or head.actor_id != ACTOR_ID
        or head.content_hash != fixture.content_hash
        or head.world_id != expected_world_id
        or head.agent_profile_id != str(launch_json["agent_profile_id"])
        or head.authority_id != str(launch_json["authority_id"])
        or head.revision < 0
    ):
        raise PersistentPlayAuthorityError("REGISTRY_HEAD_AUTHORITY_MISMATCH")


def main() -> None:
    try:
        config = Int1AuthoritySeedConfig.from_env()
        summary = asyncio.run(verify_persistent_watering_authority(config))
    except PersistentPlayAuthorityError as error:
        print(f"PERSISTENT_WATERING_AUTHORITY_INVALID code={error.code}", file=sys.stderr)
        raise SystemExit(2) from None
    except ValueError:
        print("PERSISTENT_WATERING_AUTHORITY_INVALID code=CONFIG_INVALID", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as error:  # pragma: no cover - secret-safe CLI boundary
        print(
            "PERSISTENT_WATERING_AUTHORITY_INVALID "
            f"code=VERIFIER_EXECUTION_FAILED exception_type={type(error).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(summary.as_json(), ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "CURRENT_WATERING_BUILD_POLICY_SHA256",
    "CURRENT_WATERING_CONTENT_HASH",
    "CURRENT_WATERING_LAUNCH_AUTHORITY_SHA256",
    "CURRENT_WATERING_SOURCE_BUNDLE_SHA256",
    "PersistentPlayAuthorityError",
    "PersistentPlayAuthoritySummary",
    "verify_persistent_watering_authority",
]
