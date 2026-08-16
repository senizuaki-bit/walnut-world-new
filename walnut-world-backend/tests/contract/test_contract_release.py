"""Current-Agent-workspace contract verification tests."""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from walnut_backend.bootstrap import ContractRelease, Settings
from walnut_backend.contract_release import ContractReleaseVerificationError


BACKEND_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SCRIPT = BACKEND_ROOT / "scripts" / "verify_contract_release.py"
AGENT_REPOSITORY = Path(
    os.environ.get("WALNUT_CONTRACT_PATH", str(BACKEND_ROOT.parent / "agent"))
)


def test_python_dependency_entries_pin_the_current_contract_release() -> None:
    expected = "yaya_agent_contracts==0.6.0"
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pyproject = (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert requirements == [expected]
    assert f'"{expected}",' in pyproject


@pytest.fixture
def agent_workspace(tmp_path: Path) -> Path:
    """Copy only pinned release inputs so negative tests never mutate the checkout."""
    destination = tmp_path / "agent"
    manifest_path = AGENT_REPOSITORY / "contracts" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [Path("contracts/manifest.json"), Path("python/yaya_agent_contracts/ports.py")]
    paths.extend(Path(entry["path"]) for entry in manifest["files"])
    for relative in dict.fromkeys(paths):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(AGENT_REPOSITORY / relative, target)
    return destination


def verify(agent_workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--agent-repo", str(agent_workspace)],
        cwd=BACKEND_ROOT,
        text=True,
        capture_output=True,
    )


def test_accepts_current_agent_contract_workspace(agent_workspace: Path) -> None:
    result = verify(agent_workspace)

    assert result.returncode == 0, result.stderr
    assert "release verification passed" in result.stdout.lower()


def test_rejects_missing_product_schema(agent_workspace: Path) -> None:
    (agent_workspace / "contracts/schemas/product-experience/session-workspace.schema.json").unlink()

    result = verify(agent_workspace)

    assert result.returncode != 0
    assert "missing manifested file" in result.stderr.lower()


def test_rejects_changed_manifested_wire_file(agent_workspace: Path) -> None:
    schema = agent_workspace / "contracts/schemas/game/run.schema.json"
    schema.write_bytes(schema.read_bytes() + b"\n")

    result = verify(agent_workspace)

    assert result.returncode != 0
    assert "manifested bytes differ" in result.stderr.lower()


def test_rejects_missing_ports_authority(agent_workspace: Path) -> None:
    (agent_workspace / "python/yaya_agent_contracts/ports.py").unlink()

    result = verify(agent_workspace)

    assert result.returncode != 0
    assert "ports authority" in result.stderr.lower()


def test_runtime_contract_reader_refuses_drift_before_serving(
    agent_workspace: Path,
) -> None:
    schema = agent_workspace / "contracts/schemas/game/run.schema.json"
    schema.write_bytes(schema.read_bytes() + b"\n")

    with pytest.raises(ContractReleaseVerificationError, match="manifested bytes differ"):
        ContractRelease(Settings.for_test(contract_path=agent_workspace))
