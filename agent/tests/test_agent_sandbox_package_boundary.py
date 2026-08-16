from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

import yaya_agent_backend.sandbox as legacy_native_sandbox  # noqa: E402
import yaya_agent_backend.sandbox_container as legacy_docker_sandbox  # noqa: E402
import yaya_agent_sandbox  # noqa: E402
from yaya_agent_contracts import SandboxPort  # noqa: E402
from yaya_agent_sandbox import RecoverableSandboxPort  # noqa: E402


class AgentSandboxPackageBoundaryTests(unittest.TestCase):
    def test_recoverable_port_extends_legacy_sandbox_surface_without_replacing_it(self) -> None:
        self.assertTrue(callable(getattr(SandboxPort, "run")))
        self.assertFalse(hasattr(SandboxPort, "reconcile"))
        self.assertTrue(callable(getattr(RecoverableSandboxPort, "run")))
        self.assertTrue(callable(getattr(RecoverableSandboxPort, "reconcile")))
        self.assertTrue(callable(getattr(yaya_agent_sandbox.DockerCppSandbox, "run")))
        self.assertTrue(callable(getattr(yaya_agent_sandbox.DockerCppSandbox, "reconcile")))

    def test_legacy_modules_are_identity_preserving_reexports(self) -> None:
        self.assertIs(
            legacy_docker_sandbox.DockerCppSandbox,
            yaya_agent_sandbox.DockerCppSandbox,
        )
        self.assertIs(
            legacy_native_sandbox.ProductionCppSandbox,
            yaya_agent_sandbox.ProductionCppSandbox,
        )
        self.assertIs(
            legacy_native_sandbox.ArgumentBuilder,
            yaya_agent_sandbox.ArgumentBuilder,
        )

    def test_sandbox_implementations_exist_only_in_provider_neutral_package(self) -> None:
        docker_source = (PACKAGE_ROOT / "yaya_agent_sandbox" / "docker.py").read_text(
            encoding="utf-8"
        )
        native_source = (PACKAGE_ROOT / "yaya_agent_sandbox" / "native.py").read_text(
            encoding="utf-8"
        )
        legacy_docker_source = (
            PACKAGE_ROOT / "yaya_agent_backend" / "sandbox_container.py"
        ).read_text(encoding="utf-8")
        legacy_native_source = (PACKAGE_ROOT / "yaya_agent_backend" / "sandbox.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("class DockerCppSandbox:", docker_source)
        self.assertIn("class ProductionCppSandbox:", native_source)
        self.assertNotIn("class DockerCppSandbox:", legacy_docker_source)
        self.assertNotIn("class ProductionCppSandbox:", legacy_native_source)

    def test_public_package_import_does_not_load_backend_or_database_driver(self) -> None:
        script = f"""
import sys
sys.path.insert(0, {str(PACKAGE_ROOT)!r})
import yaya_agent_sandbox

forbidden = sorted(
    name
    for name in sys.modules
    if name == "yaya_agent_backend"
    or name.startswith("yaya_agent_backend.")
    or name == "psycopg"
    or name.startswith("psycopg.")
)
if forbidden:
    raise SystemExit("forbidden imports: " + ", ".join(forbidden))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
