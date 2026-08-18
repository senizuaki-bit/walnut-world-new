from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from yaya_agent_build import CPP20_SAFE_V1_FLAGS

from walnut_backend.adapters.postgres.models import ProductContentUnitRow
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.int1_e2e_authority import (
    ACTOR_ID,
    CONTENT_UNIT_ID,
    CONTENT_VERSION,
    LEARNER_ID,
    PINNED_GCC_IMAGE,
    TENANT_ID,
    Int1AuthoritySeedConfig,
    build_int1_e2e_fixture,
)
from walnut_backend.persistent_play_authority import (
    CURRENT_WATERING_BUILD_POLICY_SHA256,
    CURRENT_WATERING_CONTENT_HASH,
    CURRENT_WATERING_LAUNCH_AUTHORITY_SHA256,
    CURRENT_WATERING_SOURCE_BUNDLE_SHA256,
    PersistentPlayAuthorityError,
    _exactly_one,
    _validate_rows,
)


def test_current_watering_fixture_matches_pinned_reuse_authority(tmp_path) -> None:
    config = _config(tmp_path)
    fixture = build_int1_e2e_fixture(config)

    assert fixture.content_hash == CURRENT_WATERING_CONTENT_HASH
    assert fixture.source_bundle_sha256 == CURRENT_WATERING_SOURCE_BUNDLE_SHA256
    assert fixture.build_policy_sha256 == CURRENT_WATERING_BUILD_POLICY_SHA256
    assert fixture.launch_authority_sha256 == CURRENT_WATERING_LAUNCH_AUTHORITY_SHA256
    assert fixture.build_policy_json["compile_flags"] == list(CPP20_SAFE_V1_FLAGS)
    _validate_rows(config, fixture, _rows(fixture, config))


def test_persistent_reuse_rejects_noncertified_compile_flags(tmp_path) -> None:
    config = _config(tmp_path)
    fixture = build_int1_e2e_fixture(config)
    rows = _rows(fixture, config)
    rows["policy"].policy_json = {
        **fixture.build_policy_json,
        "compile_flags": [*CPP20_SAFE_V1_FLAGS, "-Wno-unused-variable"],
    }

    with pytest.raises(PersistentPlayAuthorityError) as captured:
        _validate_rows(config, fixture, rows)
    assert captured.value.code == "BUILD_POLICY_FLAGS_MISMATCH"


def test_empty_authority_fails_closed_with_stable_reason_code() -> None:
    class EmptySession:
        async def scalar(self, _statement):
            return 0

    with pytest.raises(PersistentPlayAuthorityError) as captured:
        asyncio.run(_exactly_one(EmptySession(), ProductContentUnitRow))  # type: ignore[arg-type]
    assert captured.value.code == "PRODUCT_CONTENT_UNITS_ROW_COUNT_INVALID"


def _config(tmp_path) -> Int1AuthoritySeedConfig:
    settings = replace(
        Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH),
        development_auth_enabled=False,
        auth_hmac_secret="persistent-play-unit-only-secret-" + "s" * 32,
        auth_issuer="persistent-play-unit",
        auth_audience="persistent-play-unit-client",
    )
    return Int1AuthoritySeedConfig(
        settings=settings,
        artifact_root=tmp_path / "artifacts",
        sandbox_image=PINNED_GCC_IMAGE,
        provider_identifier="deepseek",
        model_identifier="deepseek-v4-flash",
        prompt_version="int1-prompt-v1",
        teaching_spec_version="agent-teaching-v1",
        world_rules_version="farm-rules-1",
        world_success_score=8,
        watering=True,
    )


def _rows(fixture, config: Int1AuthoritySeedConfig) -> dict[str, SimpleNamespace]:
    launch = fixture.launch_authority_json
    return {
        "content": SimpleNamespace(
            tenant_id=TENANT_ID,
            unit_id=CONTENT_UNIT_ID,
            version=CONTENT_VERSION,
            content_hash=fixture.content_hash,
            content_json=fixture.content_json,
        ),
        "world": SimpleNamespace(
            tenant_id=TENANT_ID,
            world_id=str(launch["world_id"]),
            actor_id=ACTOR_ID,
            content_hash=fixture.content_hash,
            snapshot_json=fixture.world_snapshot_json,
            state_hash=fixture.world_state_hash,
        ),
        "learner": SimpleNamespace(
            tenant_id=TENANT_ID,
            learner_id=LEARNER_ID,
            actor_id=ACTOR_ID,
            content_hash=fixture.content_hash,
            profile_json=fixture.learner_profile_json,
            profile_sha256=fixture.learner_profile_sha256,
        ),
        "profile": SimpleNamespace(
            tenant_id=TENANT_ID,
            agent_profile_id=str(launch["agent_profile_id"]),
            actor_id=ACTOR_ID,
            content_hash=fixture.content_hash,
            profile_json=fixture.agent_profile_json,
            profile_sha256=fixture.agent_profile_sha256,
        ),
        "policy": SimpleNamespace(
            tenant_id=TENANT_ID,
            build_policy_id=str(launch["build_policy_id"]),
            actor_id=ACTOR_ID,
            content_hash=fixture.content_hash,
            policy_json=fixture.build_policy_json,
            policy_sha256=fixture.build_policy_sha256,
            active=True,
        ),
        "launch": SimpleNamespace(
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
        ),
        "head": SimpleNamespace(
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            content_hash=fixture.content_hash,
            world_id=str(launch["world_id"]),
            agent_profile_id=str(launch["agent_profile_id"]),
            authority_id=str(launch["authority_id"]),
            revision=0,
        ),
    }
