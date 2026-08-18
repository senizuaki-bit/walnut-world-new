"""Secret-safe configuration for the private recoverable LLM relay."""

from __future__ import annotations

import hmac
import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_MAX_SECRET_FILE_BYTES = 4098
_WINDOWS_SECRET_PATH_ENV = "WALNUT_INTERNAL_SECRET_FILE_PATH"
_WINDOWS_ACL_VALIDATOR = r"""
$ErrorActionPreference = 'Stop'
$path = [Environment]::GetEnvironmentVariable(
    'WALNUT_INTERNAL_SECRET_FILE_PATH',
    'Process'
)
if ([string]::IsNullOrWhiteSpace($path)) { exit 40 }
try {
    $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
    $raw = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorBinaryForm(),
        0
    )
    $daclPresent = (
        $raw.ControlFlags -band
        [System.Security.AccessControl.ControlFlags]::DiscretionaryAclPresent
    ) -ne 0
    if (-not $daclPresent -or $null -eq $raw.DiscretionaryAcl) { exit 43 }
    $rules = $acl.GetAccessRules(
        $true,
        $true,
        [System.Security.Principal.SecurityIdentifier]
    )
}
catch { exit 41 }
$broadSids = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
$readData = [int64][System.Security.AccessControl.FileSystemRights]::ReadData
foreach ($rule in $rules) {
    $sid = [string]$rule.IdentityReference.Value
    $rights = [int64]$rule.FileSystemRights
    if (
        $rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
        $broadSids -contains $sid -and
        (($rights -band $readData) -ne 0)
    ) { exit 42 }
}
[Console]::Out.Write('SECURE')
"""


@dataclass(frozen=True, slots=True)
class RelaySettings:
    database_url: str
    relay_api_key: str = field(repr=False)
    upstream_api_key: str = field(repr=False)
    provider: str = ""
    model: str = ""
    upstream_endpoint: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8081
    allow_container_bind: bool = False
    result_retention_seconds: int = 604_800
    max_request_bytes: int = 4_194_304
    max_response_bytes: int = 2_097_152
    upstream_timeout_ms: int = 120_000
    acknowledgement_grace_seconds: int = 5
    poll_seconds: float = 0.1
    max_total_generations: int | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.database_url, "database_url", 4096)
        _secret_text(self.relay_api_key, "relay_api_key")
        _secret_text(self.upstream_api_key, "upstream_api_key")
        if hmac.compare_digest(self.relay_api_key, self.upstream_api_key):
            raise ValueError("relay bearer and upstream Provider key must be different")
        _bounded_text(self.provider, "provider", 128)
        _bounded_text(self.model, "model", 128)
        parsed = urlsplit(self.upstream_endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.rstrip("/").endswith("/chat/completions")
        ):
            raise ValueError(
                "upstream_endpoint must be an HTTPS chat/completions URL without userinfo, "
                "query, or fragment"
            )
        if self.bind_host not in {"127.0.0.1", "::1", "localhost"} and not (
            self.allow_container_bind and self.bind_host in {"0.0.0.0", "::"}
        ):
            raise ValueError("relay must bind to loopback unless container-internal bind is explicit")
        _integer(self.bind_port, "bind_port", 1, 65_535)
        _integer(
            self.result_retention_seconds,
            "result_retention_seconds",
            1,
            315_360_000,
        )
        _integer(self.max_request_bytes, "max_request_bytes", 1, 67_108_864)
        _integer(self.max_response_bytes, "max_response_bytes", 1, 67_108_864)
        _integer(self.upstream_timeout_ms, "upstream_timeout_ms", 1, 300_000)
        _integer(
            self.acknowledgement_grace_seconds,
            "acknowledgement_grace_seconds",
            0,
            300,
        )
        if not isinstance(self.poll_seconds, (int, float)) or not 0.01 <= self.poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.01 and 60")
        if self.max_total_generations is not None:
            _integer(
                self.max_total_generations,
                "max_total_generations",
                1,
                1_000_000,
            )

    @property
    def upstream_deadline_seconds(self) -> float:
        return self.upstream_timeout_ms / 1000 + self.acknowledgement_grace_seconds

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RelaySettings:
        values = os.environ if environ is None else environ
        return cls(
            database_url=_required(values, "WALNUT_DATABASE_URL"),
            relay_api_key=_secret(values, "WALNUT_LLM_RELAY_SERVER_API_KEY"),
            upstream_api_key=_secret(values, "WALNUT_LLM_UPSTREAM_API_KEY"),
            provider=_required(values, "WALNUT_LLM_PROVIDER"),
            model=_required(values, "WALNUT_LLM_MODEL"),
            upstream_endpoint=_required(values, "WALNUT_LLM_UPSTREAM_ENDPOINT"),
            bind_host=values.get("WALNUT_LLM_RELAY_BIND_HOST", "127.0.0.1"),
            bind_port=_env_integer(values, "WALNUT_LLM_RELAY_BIND_PORT", 8081),
            allow_container_bind=_env_boolean(
                values,
                "WALNUT_LLM_RELAY_ALLOW_CONTAINER_BIND",
                False,
            ),
            result_retention_seconds=_env_integer(
                values,
                "WALNUT_LLM_RELAY_RESULT_RETENTION_SECONDS",
                604_800,
            ),
            max_request_bytes=_env_integer(
                values,
                "WALNUT_LLM_RELAY_MAX_REQUEST_BYTES",
                4_194_304,
            ),
            max_response_bytes=_env_integer(
                values,
                "WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES",
                2_097_152,
            ),
            upstream_timeout_ms=_env_integer(
                values,
                "WALNUT_LLM_UPSTREAM_TIMEOUT_MS",
                120_000,
            ),
            acknowledgement_grace_seconds=_env_integer(
                values,
                "WALNUT_LLM_ACKNOWLEDGEMENT_GRACE_SECONDS",
                5,
            ),
            poll_seconds=_env_float(values, "WALNUT_LLM_RELAY_POLL_SECONDS", 0.1),
            max_total_generations=_env_optional_integer(
                values,
                "WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS",
            ),
        )


def _secret(values: Mapping[str, str], name: str) -> str:
    direct = values.get(name)
    file_name = values.get(f"{name}_FILE")
    if bool(direct) == bool(file_name):
        raise ValueError(f"set exactly one of {name} or {name}_FILE")
    if direct is not None:
        _secret_text(direct, name)
        return direct
    return _read_secret_file(Path(str(file_name)), name)


def _read_secret_file(path: Path, name: str) -> str:
    try:
        metadata = path.lstat()
    except (OSError, ValueError):
        raise ValueError(f"{name}_FILE is unavailable") from None
    if _is_reparse_or_not_regular(metadata):
        raise ValueError(f"{name}_FILE must be a regular non-reparse file")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError(f"{name}_FILE must deny group and other access")
    if metadata.st_size > _MAX_SECRET_FILE_BYTES:
        raise ValueError(f"{name}_FILE is too large")
    if os.name == "nt":
        _assert_windows_acl_denies_broad_read(path)
    try:
        with path.open("rb") as secret_file:
            opened_metadata = os.fstat(secret_file.fileno())
            if _is_reparse_or_not_regular(opened_metadata) or not _same_file(
                metadata,
                opened_metadata,
            ):
                raise ValueError(f"{name}_FILE changed during validation")
            payload = secret_file.read(_MAX_SECRET_FILE_BYTES + 1)
            final_metadata = os.fstat(secret_file.fileno())
    except ValueError:
        raise
    except (OSError, RuntimeError):
        raise ValueError(f"{name}_FILE could not be read") from None
    if len(payload) > _MAX_SECRET_FILE_BYTES:
        raise ValueError(f"{name}_FILE is too large")
    if not _same_file(opened_metadata, final_metadata) or (
        opened_metadata.st_size != final_metadata.st_size
        or opened_metadata.st_mtime_ns != final_metadata.st_mtime_ns
    ):
        raise ValueError(f"{name}_FILE changed during validation")
    try:
        value = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{name}_FILE must contain strict UTF-8 text") from error
    value = value.removesuffix("\n").removesuffix("\r")
    _secret_text(value, name)
    return value


def _is_reparse_or_not_regular(metadata: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(file_attributes & reparse_attribute) or not stat.S_ISREG(metadata.st_mode)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _assert_windows_acl_denies_broad_read(path: Path) -> None:
    validator_environment = {
        name: value
        for name in (
            "ComSpec",
            "PATH",
            "PATHEXT",
            "PSModulePath",
            "SystemRoot",
            "TEMP",
            "TMP",
            "WINDIR",
        )
        if (value := os.environ.get(name)) is not None
    }
    system_root = validator_environment.get("SystemRoot") or validator_environment.get(
        "WINDIR", r"C:\Windows"
    )
    windows_powershell_modules = str(
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
    )
    inherited_module_path = validator_environment.get("PSModulePath", "")
    module_paths = [windows_powershell_modules]
    module_paths.extend(
        entry
        for entry in inherited_module_path.split(os.pathsep)
        if entry and entry.casefold() != windows_powershell_modules.casefold()
    )
    validator_environment["PSModulePath"] = os.pathsep.join(module_paths)
    try:
        resolved_path = path.resolve(strict=True)
    except OSError:
        raise ValueError("secret file Windows ACL validation failed closed") from None
    validator_environment[_WINDOWS_SECRET_PATH_ENV] = str(resolved_path)
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_ACL_VALIDATOR,
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            env=validator_environment,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("secret file Windows ACL validation failed closed") from error
    if completed.returncode != 0 or completed.stdout != "SECURE":
        raise ValueError(
            "secret file Windows ACL must deny read data to Everyone, Users, "
            "and Authenticated Users"
        )


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _secret_text(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 4096
        or any(not character.isprintable() or character.isspace() for character in value)
    ):
        raise ValueError(f"{name} must be a bounded non-whitespace secret")


def _bounded_text(value: object, name: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{name} must be bounded printable text")


def _integer(value: object, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def _env_integer(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _env_optional_integer(values: Mapping[str, str], name: str) -> int | None:
    value = values.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _env_float(values: Mapping[str, str], name: str, default: float) -> float:
    value = values.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error


def _env_boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be boolean")


__all__ = ["RelaySettings"]
