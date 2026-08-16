from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

import yaya_agent_backend.build_pipeline as legacy_build_pipeline  # noqa: E402
import yaya_agent_build  # noqa: E402


class AgentBuildPackageBoundaryTests(unittest.TestCase):
    def test_legacy_module_is_an_identity_preserving_reexport(self) -> None:
        self.assertEqual(legacy_build_pipeline.__all__, yaya_agent_build.__all__)
        for name in yaya_agent_build.__all__:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(legacy_build_pipeline, name),
                    getattr(yaya_agent_build, name),
                )

    def test_build_implementation_exists_in_exactly_one_module(self) -> None:
        implementation = PACKAGE_ROOT / "yaya_agent_build" / "pipeline.py"
        compatibility = PACKAGE_ROOT / "yaya_agent_backend" / "build_pipeline.py"

        implementation_source = implementation.read_text(encoding="utf-8")
        compatibility_source = compatibility.read_text(encoding="utf-8")
        self.assertIn("class DigestPinnedDockerCppBuilder:", implementation_source)
        self.assertIn("class ContentAddressedArtifactPublisher:", implementation_source)
        self.assertNotIn("class DigestPinnedDockerCppBuilder:", compatibility_source)
        self.assertNotIn("class ContentAddressedArtifactPublisher:", compatibility_source)

    def test_public_package_import_does_not_load_backend_or_database_driver(self) -> None:
        script = f"""
import sys
sys.path.insert(0, {str(PACKAGE_ROOT)!r})
import yaya_agent_build

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
