from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from postgres_test_support import reset_sandbox_recovery_results
from test_agent_backend_docker_cpp_sandbox import PINNED_GCC_IMAGE


class SandboxFixtureCleanupTests(unittest.TestCase):
    def test_reset_removes_owned_leftover_container_and_preserves_other_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-cleanup-regression-") as directory:
            root = Path(directory).resolve()
            owned = root / "owned"
            foreign = root / "owned-other"
            owned.mkdir()
            foreign.mkdir()
            result_root = owned / ".sandbox-results"
            result_root.mkdir()
            (result_root / "prior.launch.json").write_text("{}", encoding="utf-8")
            containers: list[str] = []
            try:
                for owner in (owned, foreign):
                    artifact = owner / "skill"
                    artifact.write_text("fixture", encoding="utf-8")
                    name = "yaya-sbx-" + uuid.uuid4().hex[:24]
                    subprocess.run(
                        [
                            "docker",
                            "create",
                            "--pull=never",
                            "--name",
                            name,
                            "--label",
                            "local.yaya.sandbox=true",
                            "--mount",
                            f"type=bind,source={artifact},target=/opt/yaya/skill,readonly",
                            PINNED_GCC_IMAGE,
                            "true",
                        ],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    containers.append(name)
                reset_sandbox_recovery_results(result_root, owner_root=owned)
                for name, should_exist in zip(containers, (False, True), strict=True):
                    inspected = subprocess.run(
                        ["docker", "inspect", name],
                        capture_output=True,
                        timeout=30,
                    )
                    self.assertEqual(inspected.returncode == 0, should_exist)
                    if should_exist:
                        self.assertEqual(json.loads(inspected.stdout)[0]["Name"], "/" + name)
                self.assertEqual(list(result_root.iterdir()), [])
            finally:
                for name in containers:
                    subprocess.run(
                        ["docker", "rm", "--force", name],
                        capture_output=True,
                        timeout=30,
                    )


if __name__ == "__main__":
    unittest.main()
