from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.config import (  # noqa: E402
    LearnerWorkerSettings,
    ProductionSettings,
)

_IMAGE = "gcc@sha256:" + "a" * 64


def _environment() -> dict[str, str]:
    return {
        "YAYA_DATABASE_DSN": "postgresql://agent:database-secret@127.0.0.1:5432/yaya",
        "YAYA_ARTIFACT_ROOT": str((CONTRACTS_ROOT.parent / "artifacts").resolve()),
        "YAYA_CONTRACTS_ROOT": str(CONTRACTS_ROOT.resolve()),
        "YAYA_AUTH_HMAC_SECRET": "auth-secret-" + "s" * 48,
        "YAYA_AUTH_ISSUER": "yaya-config-tests",
        "YAYA_AUTH_AUDIENCE": "yaya-game-api",
        "YAYA_LLM_MODE": "provider",
        "YAYA_LLM_ENDPOINT": "https://provider.example/v1/chat/completions",
        "YAYA_LLM_API_KEY": "provider-secret-key",
        "YAYA_LLM_MODEL": "provider-model-v1",
        "YAYA_LLM_PROVIDER": "provider-name",
        "YAYA_LEARNER_WORKER_ID": "learner_worker_config_0001",
        "YAYA_LEARNER_WORKER_LEASE_SECONDS": "47",
        "YAYA_LEARNER_WORKER_POLL_MS": "275",
        "YAYA_SANDBOX_IMAGE": _IMAGE,
    }


class ProductionSettingsTests(unittest.TestCase):
    def test_learner_worker_settings_require_no_agent_runtime_dependencies(self) -> None:
        environment = {
            "YAYA_DATABASE_DSN": _environment()["YAYA_DATABASE_DSN"],
            "YAYA_CONTRACTS_ROOT": str(CONTRACTS_ROOT.resolve()),
            "YAYA_LEARNER_WORKER_ID": "learner_worker_isolated_0001",
            "YAYA_LEARNER_WORKER_LEASE_SECONDS": "41",
            "YAYA_LEARNER_WORKER_POLL_MS": "315",
        }
        settings = LearnerWorkerSettings.from_env(environment)
        self.assertEqual(settings.worker_id, "learner_worker_isolated_0001")
        self.assertEqual(settings.lease_seconds, 41)
        self.assertEqual(settings.poll_ms, 315)
        self.assertNotIn("database-secret", repr(settings))

    def test_provider_requires_endpoint_key_model_and_provider(self) -> None:
        for name in (
            "YAYA_LLM_ENDPOINT",
            "YAYA_LLM_API_KEY",
            "YAYA_LLM_MODEL",
            "YAYA_LLM_PROVIDER",
        ):
            with self.subTest(name=name):
                environment = _environment()
                environment.pop(name)
                with self.assertRaises(ValueError):
                    ProductionSettings.from_env(environment)

    def test_explicit_fallback_rejects_ambiguous_provider_configuration(self) -> None:
        environment = _environment()
        environment.update(
            {
                "YAYA_LLM_MODE": "fallback",
                "YAYA_LLM_MODEL": "explicit-fallback",
                "YAYA_LLM_PROVIDER": "explicit-fallback",
            }
        )
        environment.pop("YAYA_LLM_ENDPOINT")
        environment.pop("YAYA_LLM_API_KEY")
        settings = ProductionSettings.from_env(environment)
        self.assertEqual(settings.llm_mode, "fallback")
        self.assertIsNone(settings.llm_endpoint)
        self.assertIsNone(settings.llm_api_key)

        environment["YAYA_LLM_ENDPOINT"] = "https://provider.example/v1/chat/completions"
        with self.assertRaises(ValueError):
            ProductionSettings.from_env(environment)

    def test_repr_never_exposes_database_auth_or_provider_secrets(self) -> None:
        settings = ProductionSettings.from_env(_environment())
        rendered = repr(settings)
        self.assertNotIn("database-secret", rendered)
        self.assertNotIn("auth-secret", rendered)
        self.assertNotIn("provider-secret-key", rendered)

    def test_provider_thinking_mode_is_explicit_and_fail_loud(self) -> None:
        environment = _environment()
        settings = ProductionSettings.from_env(environment)
        self.assertIsNone(settings.llm_thinking_mode)

        environment["YAYA_LLM_THINKING_MODE"] = "disabled"
        settings = ProductionSettings.from_env(environment)
        self.assertEqual(settings.llm_thinking_mode, "disabled")

        environment["YAYA_LLM_THINKING_MODE"] = "automatic"
        with self.assertRaisesRegex(ValueError, "YAYA_LLM_THINKING_MODE"):
            ProductionSettings.from_env(environment)

        environment.update(
            {
                "YAYA_LLM_MODE": "fallback",
                "YAYA_LLM_MODEL": "explicit-fallback",
                "YAYA_LLM_PROVIDER": "explicit-fallback",
                "YAYA_LLM_THINKING_MODE": "disabled",
            }
        )
        environment.pop("YAYA_LLM_ENDPOINT")
        environment.pop("YAYA_LLM_API_KEY")
        with self.assertRaisesRegex(ValueError, "provider thinking"):
            ProductionSettings.from_env(environment)

    def test_learner_worker_has_an_independent_validated_configuration(self) -> None:
        settings = ProductionSettings.from_env(_environment())
        self.assertEqual(settings.worker_id, "worker_agent_0001")
        self.assertEqual(settings.learner_worker_id, "learner_worker_config_0001")
        self.assertEqual(settings.learner_worker_lease_seconds, 47)
        self.assertEqual(settings.learner_worker_poll_ms, 275)

        for name, value in (
            ("YAYA_LEARNER_WORKER_ID", ""),
            ("YAYA_LEARNER_WORKER_LEASE_SECONDS", "1"),
            ("YAYA_LEARNER_WORKER_POLL_MS", "9"),
        ):
            with self.subTest(name=name):
                environment = _environment()
                environment[name] = value
                with self.assertRaises(ValueError):
                    LearnerWorkerSettings.from_env(environment)


if __name__ == "__main__":
    unittest.main()
