from __future__ import annotations

from pathlib import Path

import pytest

from walnut_backend.bootstrap import Settings
from walnut_backend.image_reference import require_digest_pinned_image
from walnut_backend.worker_main import WorkerSettings


def test_production_auth_is_the_default_and_requires_complete_jwt_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "WALNUT_DEVELOPMENT_AUTH",
        "WALNUT_AUTH_HMAC_SECRET",
        "WALNUT_AUTH_ISSUER",
        "WALNUT_AUTH_AUDIENCE",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(
        ValueError, match="production JWT secret, issuer, and audience are required"
    ):
        Settings.from_env()


def test_development_auth_requires_an_explicit_true_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WALNUT_AUTH_HMAC_SECRET", "s" * 32)
    monkeypatch.setenv("WALNUT_AUTH_ISSUER", "security-test")
    monkeypatch.setenv("WALNUT_AUTH_AUDIENCE", "security-test-client")
    monkeypatch.delenv("WALNUT_DEVELOPMENT_AUTH", raising=False)
    assert not Settings.from_env().development_auth_enabled

    monkeypatch.delenv("WALNUT_AUTH_HMAC_SECRET")
    monkeypatch.delenv("WALNUT_AUTH_ISSUER")
    monkeypatch.delenv("WALNUT_AUTH_AUDIENCE")
    monkeypatch.setenv("WALNUT_DEVELOPMENT_AUTH", "true")
    assert Settings.from_env().development_auth_enabled


def test_development_auth_rejects_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WALNUT_DEVELOPMENT_AUTH", "enabled")
    with pytest.raises(ValueError, match="WALNUT_DEVELOPMENT_AUTH must be a boolean flag"):
        Settings.from_env()


def test_world_presentation_requires_an_explicit_boolean_feature_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WALNUT_DEVELOPMENT_AUTH", "true")
    monkeypatch.delenv("WALNUT_ENABLE_WORLD_PRESENTATION", raising=False)
    assert Settings.from_env().world_presentation_enabled is False

    monkeypatch.setenv("WALNUT_ENABLE_WORLD_PRESENTATION", "true")
    assert Settings.from_env().world_presentation_enabled is True

    monkeypatch.setenv("WALNUT_ENABLE_WORLD_PRESENTATION", "enabled")
    with pytest.raises(
        ValueError, match="WALNUT_ENABLE_WORLD_PRESENTATION must be a boolean flag"
    ):
        Settings.from_env()


def test_skill_patch_requires_an_explicit_boolean_feature_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WALNUT_DEVELOPMENT_AUTH", "true")
    monkeypatch.delenv("WALNUT_ENABLE_SKILL_PATCH", raising=False)
    assert Settings.from_env().skill_patch_enabled is False

    monkeypatch.setenv("WALNUT_ENABLE_SKILL_PATCH", "true")
    with pytest.raises(ValueError, match="World presentation milestone"):
        Settings.from_env()
    monkeypatch.setenv("WALNUT_ENABLE_WORLD_PRESENTATION", "true")
    assert Settings.from_env().skill_patch_enabled is True

    monkeypatch.setenv("WALNUT_ENABLE_SKILL_PATCH", "enabled")
    with pytest.raises(ValueError, match="WALNUT_ENABLE_SKILL_PATCH must be a boolean flag"):
        Settings.from_env()


@pytest.mark.parametrize(
    "value",
    (
        "gcc:14.2.0",
        "gcc:latest",
        "gcc@sha256:not-a-digest",
        "gcc@sha256:" + "A" * 64,
        "gcc @sha256:" + "a" * 64,
        "gcc@other:" + "a" * 64,
    ),
)
def test_digest_pinned_image_rejects_floating_or_malformed_references(value: str) -> None:
    with pytest.raises(ValueError, match="name@sha256"):
        require_digest_pinned_image(value, "WALNUT_SANDBOX_IMAGE")


def test_digest_pinned_image_returns_exact_runtime_digest() -> None:
    digest = "a" * 64
    assert require_digest_pinned_image(
        f"registry.example/team/compiler:14.2@sha256:{digest}",
        "WALNUT_SANDBOX_IMAGE",
    ) == ("registry.example/team/compiler:14.2", f"sha256:{digest}")


def test_worker_configuration_rejects_floating_sandbox_image_before_other_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WALNUT_SANDBOX_IMAGE", "gcc:14.2.0")
    monkeypatch.setenv("WALNUT_RUNTIME_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="WALNUT_SANDBOX_IMAGE must be name@sha256"):
        WorkerSettings.from_env()


def test_worker_skill_patch_gate_is_independently_default_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _valid_worker_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("WALNUT_ENABLE_SKILL_PATCH", raising=False)
    assert WorkerSettings.from_env().skill_patch_enabled is False

    monkeypatch.setenv("WALNUT_ENABLE_SKILL_PATCH", "true")
    with pytest.raises(ValueError, match="World presentation milestone"):
        WorkerSettings.from_env()
    monkeypatch.setenv("WALNUT_ENABLE_WORLD_PRESENTATION", "true")
    assert WorkerSettings.from_env().skill_patch_enabled is True

    monkeypatch.setenv("WALNUT_ENABLE_SKILL_PATCH", "enabled")
    with pytest.raises(
        ValueError,
        match="WALNUT_ENABLE_SKILL_PATCH must be a boolean flag",
    ):
        WorkerSettings.from_env()


def _valid_worker_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WALNUT_DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("WALNUT_TENANT_ID", "tenant_worker_test")
    monkeypatch.setenv("WALNUT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv(
        "WALNUT_SANDBOX_IMAGE",
        "registry.example/walnut/gcc:14.2@sha256:" + "a" * 64,
    )
    monkeypatch.setenv("WALNUT_LLM_RELAY_ENDPOINT", "http://127.0.0.1:8792/v1/responses")
    monkeypatch.setenv("WALNUT_LLM_RELAY_API_KEY", "r" * 32)
    monkeypatch.setenv("WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST", "true")
    monkeypatch.setenv("WALNUT_LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("WALNUT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("WALNUT_PROMPT_VERSION", "prompt-worker-v1")
    monkeypatch.setenv("WALNUT_TEACHING_SPEC_VERSION", "teaching-worker-v1")
    monkeypatch.setenv("WALNUT_WORLD_RULES_VERSION", "rules-worker-v1")
    monkeypatch.setenv("WALNUT_WORLD_CONTENT_VERSION", "content-worker-v1")
