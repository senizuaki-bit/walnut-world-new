from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from shutil import rmtree

from tests import test_agent_backend_public_role_live_e2e as public_role_live


class PublicRoleLiveArtifactClassificationTests(unittest.TestCase):
    def test_published_artifacts_are_distinct_from_durable_sandbox_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            artifact_digests = ("a" * 64, "b" * 64)
            expected: list[Path] = []
            for digest in artifact_digests:
                target = root / digest[:2] / digest
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(digest.encode("ascii"))
                expected.append(target)

            sandbox_root = root / ".sandbox-results" / "cc"
            sandbox_root.mkdir(parents=True)
            for index in range(8):
                (sandbox_root / f"receipt-{index}.json").write_text("{}", encoding="utf-8")
            workspace = root / ".build-workspaces" / "build-1" / "skill"
            workspace.parent.mkdir(parents=True)
            workspace.write_bytes(b"transient")

            self.assertEqual(
                public_role_live.PublicStudentChainRoleLiveE2E._published_artifact_files(root),
                expected,
            )
            rmtree(root / ".build-workspaces")
            fingerprint = public_role_live.PublicStudentChainRoleLiveE2E._artifact_fingerprint(root)
            self.assertEqual(len(fingerprint), 10)
            self.assertEqual(
                len([path for path in fingerprint if path.startswith(".sandbox-results/")]),
                8,
            )


if __name__ == "__main__":
    unittest.main()
