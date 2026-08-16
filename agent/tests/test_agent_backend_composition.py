from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.__main__ import _learner_worker  # noqa: E402
from yaya_agent_backend.composition import (  # noqa: E402
    ExplicitFallbackLlmAdapter,
    create_learner_worker_composition,
    create_production_composition,
    verify_contract_manifest,
)
from yaya_agent_backend.config import (  # noqa: E402
    LearnerWorkerSettings,
    ProductionSettings,
)
from yaya_agent_backend.learner_projection import LearnerProjectionWorker  # noqa: E402
from yaya_agent_backend.product_application import (  # noqa: E402
    ProductInteractionReadApplication,
)
from yaya_agent_backend.product_repositories import (  # noqa: E402
    PostgresProductInteractionReadRepository,
)
from yaya_agent_backend.stores import PostgresLearnerStore  # noqa: E402
from yaya_agent_sandbox import DockerCppSandbox, ProductionCppSandbox  # noqa: E402

PINNED_GCC_IMAGE = "gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c"


def _copy_contract_repository(raw_root: str) -> tuple[Path, Path]:
    repository_copy = Path(raw_root).resolve() / "repository"
    copied_contracts = repository_copy / "contracts"
    shutil.copytree(CONTRACTS_ROOT, copied_contracts)
    return repository_copy, copied_contracts


def _refresh_current_manifest_entry(
    repository_root: Path,
    contracts_root: Path,
    relative: str,
) -> None:
    manifest_path = contracts_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    payload = (repository_root / relative).read_bytes()
    entry["bytes"] = len(payload)
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(f"{json.dumps(manifest, indent=2)}\n", encoding="utf-8")


def _fallback_settings(artifact_root: Path) -> ProductionSettings:
    return ProductionSettings(
        database_dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        artifact_root=artifact_root,
        contracts_root=CONTRACTS_ROOT,
        auth_hmac_secret="composition-test-secret-0000000000000000",
        auth_issuer="yaya-composition-test",
        auth_audience="yaya-agent-test",
        llm_mode="fallback",
        llm_endpoint=None,
        llm_api_key=None,
        llm_model="explicit-fallback",
        llm_provider="explicit-fallback",
        llm_response_format="json_object",
        llm_max_response_bytes=2_097_152,
        allow_insecure_llm_localhost=False,
        http_host="127.0.0.1",
        http_port=8080,
        worker_id="worker_composition_0001",
        worker_lease_seconds=30,
        worker_poll_ms=100,
        sandbox_wall_ms=2_000,
        sandbox_cpu_ms=1_000,
        sandbox_memory_bytes=67_108_864,
        sandbox_max_intents=64,
        sandbox_max_output_bytes=65_536,
        sandbox_max_processes=1,
        sandbox_image=PINNED_GCC_IMAGE,
        docker_executable="docker",
        learner_worker_id="learner_worker_composition_0001",
        learner_worker_lease_seconds=43,
        learner_worker_poll_ms=275,
    )


def _learner_settings() -> LearnerWorkerSettings:
    return LearnerWorkerSettings(
        database_dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        contracts_root=CONTRACTS_ROOT,
        worker_id="learner_worker_composition_0001",
        lease_seconds=43,
        poll_ms=275,
    )


class AgentBackendCompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_current_v06_manifest_and_all_previous_release_locks_are_accepted(self) -> None:
        verify_contract_manifest(CONTRACTS_ROOT)

    def test_contract_manifest_tampering_fails_before_adapter_construction(self) -> None:
        verify_contract_manifest(CONTRACTS_ROOT)
        with tempfile.TemporaryDirectory(prefix="yaya-contract-tamper-") as raw_root:
            repository_copy, copied_contracts = _copy_contract_repository(raw_root)
            manifest = json.loads((copied_contracts / "manifest.json").read_text(encoding="utf-8"))
            first_entry = manifest["files"][0]
            target = repository_copy / first_entry["path"]
            target.write_bytes(target.read_bytes() + b"\n")

            with self.assertRaisesRegex(RuntimeError, "contract file hash drifted"):
                verify_contract_manifest(copied_contracts)

    def test_unlisted_current_wire_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-contract-unlisted-") as raw_root:
            _, copied_contracts = _copy_contract_repository(raw_root)
            unlisted = copied_contracts / "schemas" / "unlisted.json"
            unlisted.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "current manifest inventory drifted"):
                verify_contract_manifest(copied_contracts)

    def test_regenerated_current_manifest_cannot_hide_v04_or_v05_file_drift(self) -> None:
        cases = (
            (
                "v0.4",
                "contracts/schemas/game/student-bootstrap-v2.schema.json",
            ),
            (
                "v0.5",
                "contracts/schemas/game/world-presentation-event.schema.json",
            ),
        )
        for label, relative in cases:
            with self.subTest(release=label):
                with tempfile.TemporaryDirectory(
                    prefix=f"yaya-contract-{label}-file-drift-"
                ) as raw_root:
                    repository_copy, copied_contracts = _copy_contract_repository(raw_root)
                    target = repository_copy / relative
                    target.write_bytes(target.read_bytes() + b"\n")
                    _refresh_current_manifest_entry(
                        repository_copy,
                        copied_contracts,
                        relative,
                    )

                    with self.assertRaisesRegex(
                        RuntimeError,
                        rf"{re.escape(label)} frozen file drifted",
                    ):
                        verify_contract_manifest(copied_contracts)

    def test_regenerated_current_manifest_cannot_hide_previous_lock_byte_drift(self) -> None:
        for label in ("v0.3", "v0.4", "v0.5"):
            with self.subTest(release=label):
                with tempfile.TemporaryDirectory(
                    prefix=f"yaya-contract-{label}-lock-drift-"
                ) as raw_root:
                    repository_copy, copied_contracts = _copy_contract_repository(raw_root)
                    relative = f"contracts/releases/agent-contracts-{label}.lock.json"
                    lock_path = repository_copy / relative
                    lock_path.write_bytes(lock_path.read_bytes() + b"\n")
                    _refresh_current_manifest_entry(
                        repository_copy,
                        copied_contracts,
                        relative,
                    )

                    with self.assertRaisesRegex(
                        RuntimeError,
                        rf"{re.escape(label)} baseline lock bytes drifted",
                    ):
                        verify_contract_manifest(copied_contracts)

    async def test_fallback_composition_uses_only_pinned_docker_sandbox(self) -> None:
        signature = inspect.signature(create_production_composition)
        self.assertNotIn("sandbox", signature.parameters)
        with tempfile.TemporaryDirectory(prefix="yaya-composition-artifacts-") as raw_root:
            artifact_root = Path(raw_root).resolve()
            composition = await create_production_composition(
                _fallback_settings(artifact_root),
                migrate=False,
            )

        self.assertIs(type(composition.sandbox), DockerCppSandbox)
        self.assertNotIsInstance(composition.sandbox, ProductionCppSandbox)
        self.assertIs(
            type(getattr(composition.runtime, "_llm")),
            ExplicitFallbackLlmAdapter,
        )
        self.assertEqual(composition.settings.llm_mode, "fallback")
        self.assertIsNone(composition.settings.llm_api_key)
        self.assertIs(type(composition.learner_store), PostgresLearnerStore)
        self.assertIs(type(composition.learner_worker), LearnerProjectionWorker)
        self.assertIs(
            type(composition.product_repository),
            PostgresProductInteractionReadRepository,
        )
        self.assertIs(
            type(composition.product_application),
            ProductInteractionReadApplication,
        )
        self.assertIs(
            getattr(composition.product_repository, "_database"),
            composition.database,
        )
        self.assertIs(
            getattr(composition.product_repository, "_validator"),
            composition.validator,
        )
        self.assertIs(
            getattr(composition.product_application, "_repository"),
            composition.product_repository,
        )
        self.assertIs(
            getattr(composition.product_application, "_validator"),
            composition.validator,
        )
        self.assertIs(
            getattr(composition.learner_worker, "_learner"),
            composition.learner_store,
        )
        self.assertEqual(
            getattr(composition.learner_worker, "_worker_id"),
            "learner_worker_composition_0001",
        )
        self.assertEqual(getattr(composition.learner_worker, "_lease_seconds"), 43)
        self.assertEqual(getattr(composition.learner_worker, "_poll_ms"), 275)
        contexts = getattr(composition.hub, "_contexts")
        self.assertEqual(
            getattr(contexts, "_teaching_spec_version"),
            "agent-teaching-v1",
        )

    async def test_learner_worker_composition_is_dependency_minimal(self) -> None:
        composition = await create_learner_worker_composition(
            _learner_settings(),
            migrate=False,
        )
        self.assertIs(type(composition.learner_store), PostgresLearnerStore)
        self.assertIs(type(composition.learner_worker), LearnerProjectionWorker)
        self.assertFalse(hasattr(composition, "runtime"))
        self.assertFalse(hasattr(composition, "sandbox"))
        self.assertFalse(hasattr(composition, "authenticator"))

    async def test_learner_worker_cancellation_drains_through_stop_event(self) -> None:
        entered = asyncio.Event()
        stopped = asyncio.Event()
        child_was_cancelled = False

        class ObservedLearnerWorker:
            async def run_forever(self, stop: asyncio.Event) -> None:
                nonlocal child_was_cancelled
                entered.set()
                try:
                    await stop.wait()
                    stopped.set()
                except asyncio.CancelledError:
                    child_was_cancelled = True
                    raise

        composition = SimpleNamespace(learner_worker=ObservedLearnerWorker())
        factory = AsyncMock(return_value=composition)
        with patch(
            "yaya_agent_backend.__main__.create_learner_worker_composition",
            factory,
        ):
            service = asyncio.create_task(
                _learner_worker(_learner_settings()),
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            service.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(service, timeout=1)

        self.assertTrue(stopped.is_set())
        self.assertFalse(child_was_cancelled)


if __name__ == "__main__":
    unittest.main()
