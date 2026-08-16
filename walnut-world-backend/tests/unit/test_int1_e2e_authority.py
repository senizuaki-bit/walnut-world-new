from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from yaya_agent_build import CPP20_SAFE_V1_PROFILE, canonical_source_bundle_sha256
from yaya_agent_contracts import HarvestIntent, canonical_json_sha256

from walnut_backend.bootstrap import Settings
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.int1_e2e_authority import (
    ACTOR_ID,
    PINNED_GCC_IMAGE,
    Int1AuthoritySeedConfig,
    Int1AuthoritySeedError,
    _issue_student_authorization,
    build_int1_e2e_fixture,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = BACKEND_ROOT.parent / "agent"
JWT_SECRET = "int1-test-only-hs256-secret-value"
FORMAL_M2_PHASE_DEADLINE_SECONDS = 600
FORMAL_M2_TRANSITION_BUDGET_SECONDS = 300
FORMAL_M2_TOKEN_LIFETIME_SECONDS = 1800


def test_int1_token_covers_complete_formal_m2_budget_and_refuses_a_lower_auth_cap(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    authorization = _issue_student_authorization(config.settings)
    encoded_claims = authorization.removeprefix("Bearer ").split(".")[1]
    claims = json.loads(
        base64.urlsafe_b64decode(encoded_claims + "=" * (-len(encoded_claims) % 4))
    )

    lifetime_seconds = claims["exp"] - claims["iat"]
    required_seconds = (
        2 * FORMAL_M2_PHASE_DEADLINE_SECONDS + FORMAL_M2_TRANSITION_BUDGET_SECONDS
    )
    assert lifetime_seconds == FORMAL_M2_TOKEN_LIFETIME_SECONDS
    assert lifetime_seconds >= required_seconds

    lowered_settings = replace(
        config.settings,
        auth_maximum_lifetime_seconds=FORMAL_M2_TOKEN_LIFETIME_SECONDS - 1,
    )
    with pytest.raises(Int1AuthoritySeedError, match="maximum JWT lifetime"):
        _issue_student_authorization(lowered_settings)


def test_int1_fixture_is_deterministic_canonical_and_agent_compatible(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = build_int1_e2e_fixture(config)
    second = build_int1_e2e_fixture(config)

    assert first == second
    starter = first.content_json["task"]["starter_skill"]
    source_bundle = starter["source_bundle"]
    assert starter["compiler_profile"] == CPP20_SAFE_V1_PROFILE
    assert canonical_source_bundle_sha256(source_bundle) == first.source_bundle_sha256
    assert canonical_json_sha256(first.world_snapshot_json["state"]) == first.world_state_hash
    assert canonical_json_sha256(first.learner_profile_json) == first.learner_profile_sha256
    assert canonical_json_sha256(first.agent_profile_json) == first.agent_profile_sha256
    assert canonical_json_sha256(first.build_policy_json) == first.build_policy_sha256
    assert canonical_json_sha256(first.launch_authority_json) == first.launch_authority_sha256
    assert first.world_snapshot_json["revision"] == 0
    assert first.world_snapshot_json["last_event_sequence"] == 0
    assert first.build_policy_json["compiler_image"] == PINNED_GCC_IMAGE
    assert first.build_policy_json["parameter_schema"] == {
        "type": "object",
        "additionalProperties": False,
        "required": ["length"],
        "properties": {"length": {"type": "integer", "const": 8}},
    }
    assert first.build_policy_json["public_tests"][0]["test_case_id"] == "public_exact_io_0001"
    assert first.build_policy_json["hidden_tests"][0]["test_case_id"] == "hidden_exact_io_0001"
    expected_output = _harvest_output(8)
    expected_hash = hashlib.sha256(expected_output).hexdigest()
    assert expected_hash == "a1a2ea6960da4177c35d24c6ed7b0b47edc88a00b26f9a9e3745ae5f4846c082"
    for test_case in (
        first.build_policy_json["public_tests"][0],
        first.build_policy_json["hidden_tests"][0],
    ):
        assert test_case["arguments"] == ["8"]
        assert test_case["stdin_base64"] == ""
        assert test_case["expected_stdout_sha256"] == expected_hash
    source = source_bundle["files"][0]["content"]
    assert 'expected_world_revision\\":0' in source
    assert "std::getline" not in source
    assert all(
        plot["crop"] is not None and plot["crop"]["ready_to_harvest"] is True
        for plot in first.world_snapshot_json["state"]["plots"]
    )
    decoded = json.loads(expected_output)
    intents = tuple(
        HarvestIntent(
            intent_id=value["intent_id"],
            actor_entity_id=value["actor_entity_id"],
            expected_world_revision=value["expected_world_revision"],
            plot_id=value["plot_id"],
        )
        for value in decoded["actions"]
    )
    rules = WorldRules("1.0.0", 8, 0, 31, 0, 31, 2, 8)
    incomplete = WorldEngine().apply(first.world_snapshot_json["state"], intents[:-1], rules)
    transition = WorldEngine().apply(first.world_snapshot_json["state"], intents, rules)
    assert not incomplete.success
    assert incomplete.score == 7
    assert incomplete.applied_intent_ids == tuple(intent.intent_id for intent in intents[:-1])
    assert transition.success
    assert transition.score == 8
    assert transition.applied_intent_ids == tuple(intent.intent_id for intent in intents)
    assert all(plot["crop"] is None for plot in transition.state["plots"])


def test_env_loader_requires_opt_in_production_auth_and_never_captures_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WALNUT_LLM_RELAY_API_KEY", "must-never-enter-the-seed")
    monkeypatch.delenv("WALNUT_INT1_E2E_SEED", raising=False)
    with pytest.raises(ValueError, match="opt in"):
        Int1AuthoritySeedConfig.from_env()

    monkeypatch.setenv("WALNUT_INT1_E2E_SEED", "true")
    config = Int1AuthoritySeedConfig.from_env()
    fixture = build_int1_e2e_fixture(config)
    serialized = json.dumps(
        {
            "config": repr(config),
            "content": fixture.content_json,
            "learner": fixture.learner_profile_json,
            "agent": fixture.agent_profile_json,
            "policy": fixture.build_policy_json,
            "authority": fixture.launch_authority_json,
        },
        sort_keys=True,
    )
    assert "must-never-enter-the-seed" not in serialized
    assert not config.settings.development_auth_enabled
    assert config.settings.auth_hmac_secret == JWT_SECRET
    assert JWT_SECRET not in serialized
    assert ACTOR_ID in fixture.agent_profile_json["actor_id"]


def test_env_loader_rejects_development_auth_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WALNUT_INT1_E2E_SEED", "true")
    monkeypatch.setenv("WALNUT_DEVELOPMENT_AUTH", "true")
    with pytest.raises(ValueError, match="WALNUT_DEVELOPMENT_AUTH=false"):
        Int1AuthoritySeedConfig.from_env()


def test_env_loader_rejects_lowered_world_success_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WALNUT_INT1_E2E_SEED", "true")
    monkeypatch.setenv("WALNUT_WORLD_SUCCESS_SCORE", "7")
    with pytest.raises(ValueError, match="WORLD_SUCCESS_SCORE must be 8"):
        Int1AuthoritySeedConfig.from_env()


def _config(tmp_path: Path) -> Int1AuthoritySeedConfig:
    settings = replace(
        Settings.for_test(contract_path=AGENT_ROOT),
        development_auth_enabled=False,
        auth_hmac_secret=JWT_SECRET,
        auth_issuer="walnut-int1-test",
        auth_audience="walnut-int1-client",
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
    )


def _seed_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WALNUT_DATABASE_URL", "postgresql://test/walnut_int1")
    monkeypatch.setenv("WALNUT_CONTRACT_PATH", str(AGENT_ROOT))
    monkeypatch.setenv("WALNUT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("WALNUT_DEVELOPMENT_AUTH", "false")
    monkeypatch.setenv("WALNUT_AUTH_HMAC_SECRET", JWT_SECRET)
    monkeypatch.setenv("WALNUT_AUTH_ISSUER", "walnut-int1-test")
    monkeypatch.setenv("WALNUT_AUTH_AUDIENCE", "walnut-int1-client")
    monkeypatch.setenv("WALNUT_SANDBOX_IMAGE", PINNED_GCC_IMAGE)
    monkeypatch.setenv("WALNUT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("WALNUT_LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("WALNUT_PROMPT_VERSION", "int1-prompt-v1")
    monkeypatch.setenv("WALNUT_TEACHING_SPEC_VERSION", "agent-teaching-v1")
    monkeypatch.setenv("WALNUT_WORLD_RULES_VERSION", "farm-rules-1")
    monkeypatch.setenv("WALNUT_WORLD_SUCCESS_SCORE", "8")


def _harvest_output(length: int) -> bytes:
    return json.dumps(
        {
            "actions": [
                {
                    "intent_id": f"intent_harvest_{index:04d}",
                    "action_type": "HARVEST",
                    "actor_entity_id": "avatar_0001",
                    "expected_world_revision": 0,
                    "plot_id": f"plot_{index:04d}",
                }
                for index in range(1, length + 1)
            ]
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
