"""Byte-pinned Agent contract release authority used at process startup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RELEASE = BACKEND_ROOT / "contract-release.json"


class ContractReleaseVerificationError(Exception):
    """The configured Agent contract release is incomplete or altered."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ContractReleaseVerificationError(f"missing JSON file: {path}") from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractReleaseVerificationError(
            f"invalid UTF-8 JSON file: {path}"
        ) from error
    if not isinstance(value, dict):
        raise ContractReleaseVerificationError(f"JSON object required: {path}")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise ContractReleaseVerificationError(f"{label} path must be a string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContractReleaseVerificationError(
            f"{label} path escapes the Agent release: {value}"
        )
    return path


def _release_descriptor(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    required = {
        "package_version",
        "git_release",
        "manifest_bytes",
        "manifest_sha256",
        "manifest_file_count",
    }
    if set(value) != required:
        raise ContractReleaseVerificationError(
            "contract release descriptor is not a closed object"
        )
    if value["package_version"] != "0.6.0":
        raise ContractReleaseVerificationError(
            "backend must consume Agent contracts 0.6.0"
        )
    if value["git_release"] != "refs/tags/agent-contracts-v0.6.0":
        raise ContractReleaseVerificationError("backend Agent contract tag pin is invalid")
    if (
        isinstance(value["manifest_bytes"], bool)
        or not isinstance(value["manifest_bytes"], int)
        or value["manifest_bytes"] <= 0
        or not isinstance(value["manifest_sha256"], str)
        or len(value["manifest_sha256"]) != 64
        or isinstance(value["manifest_file_count"], bool)
        or not isinstance(value["manifest_file_count"], int)
        or value["manifest_file_count"] <= 0
    ):
        raise ContractReleaseVerificationError(
            "contract release descriptor fields are invalid"
        )
    return value


def verify_agent_contract_release(
    agent_repository: Path, release_path: Path = DEFAULT_RELEASE
) -> int:
    """Verify the exact manifest and every byte it declares before serving traffic."""

    root = agent_repository.resolve()
    expected = _release_descriptor(release_path.resolve())
    manifest_path = root / "contracts" / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        raise ContractReleaseVerificationError(
            f"cannot read Agent manifest: {manifest_path}"
        ) from error
    if len(manifest_bytes) != expected["manifest_bytes"]:
        raise ContractReleaseVerificationError(
            "Agent manifest byte count differs from the backend pin"
        )
    if _sha256(manifest_bytes) != expected["manifest_sha256"]:
        raise ContractReleaseVerificationError(
            "Agent manifest SHA-256 differs from the backend pin"
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractReleaseVerificationError(
            "Agent manifest is not strict UTF-8 JSON"
        ) from error
    if not isinstance(manifest, dict):
        raise ContractReleaseVerificationError("Agent manifest must be a JSON object")
    if manifest.get("package_version") != expected["package_version"]:
        raise ContractReleaseVerificationError(
            "Agent manifest package version differs from the backend pin"
        )
    if manifest.get("git_release") != expected["git_release"]:
        raise ContractReleaseVerificationError(
            "Agent manifest release tag differs from the backend pin"
        )
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != expected["manifest_file_count"]:
        raise ContractReleaseVerificationError(
            "Agent manifest file count differs from the backend pin"
        )

    seen: set[Path] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ContractReleaseVerificationError(
                "manifest file entry must be an object"
            )
        relative = _relative_path(entry.get("path"), label="manifest file")
        if relative in seen:
            raise ContractReleaseVerificationError(
                f"manifest declares a duplicate path: {relative}"
            )
        seen.add(relative)
        try:
            contents = (root / relative).read_bytes()
        except OSError as error:
            raise ContractReleaseVerificationError(
                f"missing manifested file: {relative}"
            ) from error
        if len(contents) != entry.get("bytes") or _sha256(contents) != entry.get(
            "sha256"
        ):
            raise ContractReleaseVerificationError(
                f"manifested bytes differ for {relative}"
            )

    required_paths = {
        Path("contracts/openapi/student-bootstrap-v2.openapi.json"),
        Path("contracts/schemas/game/student-bootstrap-v2.schema.json"),
        Path("contracts/releases/agent-contracts-v0.3.lock.json"),
        Path("contracts/releases/agent-contracts-v0.4.lock.json"),
        Path("contracts/openapi/int2-world-presentation.openapi.json"),
        Path("contracts/schemas/game/world-presentation-event.schema.json"),
        Path("contracts/schemas/game/world-presentation-event-page.schema.json"),
    }
    if not required_paths <= seen:
        raise ContractReleaseVerificationError(
            "Agent release is missing the additive INT1/INT2 or historical lock authority"
        )
    ports = root / "python" / "yaya_agent_contracts" / "ports.py"
    try:
        port_source = ports.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractReleaseVerificationError(
            "Agent Ports authority is missing or invalid"
        ) from error
    if "OperationContext" not in port_source or "class SandboxPort" not in port_source:
        raise ContractReleaseVerificationError("Agent Ports authority is incomplete")
    return len(files)


__all__ = [
    "ContractReleaseVerificationError",
    "DEFAULT_RELEASE",
    "verify_agent_contract_release",
]
