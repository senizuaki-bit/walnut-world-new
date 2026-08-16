"""Fail-loud production configuration for the Agent backend."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit


def _required(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _integer(
    source: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = source.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(source: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = source.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class LearnerWorkerSettings:
    """Dependency-minimal configuration for the learner projection process."""

    database_dsn: str = field(repr=False)
    contracts_root: Path
    worker_id: str
    lease_seconds: int
    poll_ms: int

    def __post_init__(self) -> None:
        database = urlsplit(self.database_dsn)
        if database.scheme not in {"postgresql", "postgres"} or not database.hostname:
            raise ValueError("YAYA_DATABASE_DSN must be an absolute PostgreSQL DSN")
        if not self.contracts_root.is_absolute() or not self.contracts_root.is_dir():
            raise ValueError("YAYA_CONTRACTS_ROOT must identify an absolute contract directory")
        if not self.worker_id.strip() or len(self.worker_id) > 128:
            raise ValueError("learner worker_id must contain 1..128 characters")
        if not 2 <= self.lease_seconds <= 3600:
            raise ValueError("learner lease_seconds must be between 2 and 3600")
        if not 10 <= self.poll_ms <= 60_000:
            raise ValueError("learner poll_ms must be between 10 and 60000")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LearnerWorkerSettings:
        source = os.environ if env is None else env
        return cls(
            database_dsn=_required(source, "YAYA_DATABASE_DSN"),
            contracts_root=Path(_required(source, "YAYA_CONTRACTS_ROOT")).expanduser().resolve(),
            worker_id=source.get(
                "YAYA_LEARNER_WORKER_ID",
                "learner_worker_0001",
            ).strip(),
            lease_seconds=_integer(
                source,
                "YAYA_LEARNER_WORKER_LEASE_SECONDS",
                30,
                minimum=2,
                maximum=3600,
            ),
            poll_ms=_integer(
                source,
                "YAYA_LEARNER_WORKER_POLL_MS",
                100,
                minimum=10,
                maximum=60_000,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProductionSettings:
    database_dsn: str = field(repr=False)
    artifact_root: Path
    contracts_root: Path
    auth_hmac_secret: str = field(repr=False)
    auth_issuer: str
    auth_audience: str
    llm_mode: Literal["provider", "fallback"]
    llm_endpoint: str | None
    llm_api_key: str | None = field(repr=False)
    llm_model: str
    llm_provider: str
    llm_response_format: Literal["json_object", "json_schema"]
    llm_max_response_bytes: int
    allow_insecure_llm_localhost: bool
    http_host: str
    http_port: int
    worker_id: str
    worker_lease_seconds: int
    worker_poll_ms: int
    sandbox_wall_ms: int
    sandbox_cpu_ms: int
    sandbox_memory_bytes: int
    sandbox_max_intents: int
    sandbox_max_output_bytes: int
    sandbox_max_processes: int
    sandbox_image: str
    docker_executable: str
    learner_worker_id: str = "learner_worker_0001"
    learner_worker_lease_seconds: int = 30
    learner_worker_poll_ms: int = 100
    llm_thinking_mode: Literal["enabled", "disabled"] | None = None

    def __post_init__(self) -> None:
        database = urlsplit(self.database_dsn)
        if database.scheme not in {"postgresql", "postgres"} or not database.hostname:
            raise ValueError("YAYA_DATABASE_DSN must be an absolute PostgreSQL DSN")
        if not self.artifact_root.is_absolute():
            raise ValueError("YAYA_ARTIFACT_ROOT must resolve to an absolute path")
        if not self.contracts_root.is_absolute() or not self.contracts_root.is_dir():
            raise ValueError("YAYA_CONTRACTS_ROOT must identify an absolute contract directory")
        if not 32 <= len(self.auth_hmac_secret) <= 4096:
            raise ValueError("YAYA_AUTH_HMAC_SECRET must contain 32..4096 characters")
        if self.llm_mode == "provider":
            if (
                self.llm_endpoint is None
                or self.llm_api_key is None
                or self.llm_model == "explicit-fallback"
                or self.llm_provider == "explicit-fallback"
            ):
                raise ValueError(
                    "provider mode requires endpoint, API key, model and provider identity"
                )
        elif self.llm_mode != "fallback":
            raise ValueError("YAYA_LLM_MODE must be provider or fallback")
        elif (
            self.llm_endpoint is not None
            or self.llm_api_key is not None
            or self.llm_model != "explicit-fallback"
            or self.llm_provider != "explicit-fallback"
        ):
            raise ValueError("fallback mode must use only the explicit-fallback identity")
        if self.llm_response_format not in {"json_object", "json_schema"}:
            raise ValueError("YAYA_LLM_RESPONSE_FORMAT is unsupported")
        if self.llm_thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("YAYA_LLM_THINKING_MODE is unsupported")
        if self.llm_mode == "fallback" and self.llm_thinking_mode is not None:
            raise ValueError("fallback mode cannot configure provider thinking")
        if self.http_host not in {"127.0.0.1", "::1", "0.0.0.0"}:
            raise ValueError("YAYA_HTTP_HOST must be an explicit local bind address")
        if re.fullmatch(r"[a-z0-9./:_-]+@sha256:[a-f0-9]{64}", self.sandbox_image) is None:
            raise ValueError("YAYA_SANDBOX_IMAGE must be pinned by sha256 digest")
        for name in (
            "auth_issuer",
            "auth_audience",
            "llm_model",
            "llm_provider",
            "worker_id",
            "learner_worker_id",
            "docker_executable",
        ):
            value = getattr(self, name)
            if not 1 <= len(value) <= 128:
                raise ValueError(f"{name} must contain 1..128 characters")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProductionSettings:
        source = os.environ if env is None else env
        artifact_root = Path(_required(source, "YAYA_ARTIFACT_ROOT")).expanduser().resolve()
        contracts_root = Path(_required(source, "YAYA_CONTRACTS_ROOT")).expanduser().resolve()
        llm_mode_raw = source.get("YAYA_LLM_MODE", "provider").strip().lower()
        if llm_mode_raw not in {"provider", "fallback"}:
            raise ValueError("YAYA_LLM_MODE must be provider or fallback")
        llm_mode: Literal["provider", "fallback"] = (
            "provider" if llm_mode_raw == "provider" else "fallback"
        )
        key = source.get("YAYA_LLM_API_KEY", "").strip() or None
        key_file_raw = source.get("YAYA_LLM_API_KEY_FILE", "").strip()
        if key is not None and key_file_raw:
            raise ValueError("set only one of YAYA_LLM_API_KEY and YAYA_LLM_API_KEY_FILE")
        if key_file_raw:
            key_file = Path(key_file_raw).expanduser().resolve()
            if not key_file.is_file():
                raise ValueError("YAYA_LLM_API_KEY_FILE does not identify a file")
            key = key_file.read_text(encoding="utf-8").strip()
        endpoint = source.get("YAYA_LLM_ENDPOINT", "").strip() or None
        response_format_raw = source.get("YAYA_LLM_RESPONSE_FORMAT", "json_object").strip()
        if response_format_raw not in {"json_object", "json_schema"}:
            raise ValueError("YAYA_LLM_RESPONSE_FORMAT is unsupported")
        response_format: Literal["json_object", "json_schema"] = (
            "json_schema" if response_format_raw == "json_schema" else "json_object"
        )
        thinking_mode_raw = source.get("YAYA_LLM_THINKING_MODE", "").strip().lower()
        if thinking_mode_raw not in {"", "enabled", "disabled"}:
            raise ValueError("YAYA_LLM_THINKING_MODE is unsupported")
        thinking_mode: Literal["enabled", "disabled"] | None
        if thinking_mode_raw == "enabled":
            thinking_mode = "enabled"
        elif thinking_mode_raw == "disabled":
            thinking_mode = "disabled"
        else:
            thinking_mode = None
        if llm_mode == "provider":
            llm_model = _required(source, "YAYA_LLM_MODEL")
            llm_provider = _required(source, "YAYA_LLM_PROVIDER")
        else:
            llm_model = source.get("YAYA_LLM_MODEL", "explicit-fallback").strip()
            llm_provider = source.get("YAYA_LLM_PROVIDER", "explicit-fallback").strip()
        return cls(
            database_dsn=_required(source, "YAYA_DATABASE_DSN"),
            artifact_root=artifact_root,
            contracts_root=contracts_root,
            auth_hmac_secret=_required(source, "YAYA_AUTH_HMAC_SECRET"),
            auth_issuer=_required(source, "YAYA_AUTH_ISSUER"),
            auth_audience=_required(source, "YAYA_AUTH_AUDIENCE"),
            llm_mode=llm_mode,
            llm_endpoint=endpoint,
            llm_api_key=key,
            llm_model=llm_model,
            llm_provider=llm_provider,
            llm_response_format=response_format,
            llm_thinking_mode=thinking_mode,
            llm_max_response_bytes=_integer(
                source,
                "YAYA_LLM_MAX_RESPONSE_BYTES",
                2_097_152,
                minimum=1,
                maximum=16_777_216,
            ),
            allow_insecure_llm_localhost=_boolean(
                source,
                "YAYA_LLM_ALLOW_INSECURE_LOCALHOST",
            ),
            http_host=source.get("YAYA_HTTP_HOST", "127.0.0.1").strip(),
            http_port=_integer(source, "YAYA_HTTP_PORT", 8080, minimum=1, maximum=65_535),
            worker_id=source.get("YAYA_WORKER_ID", "worker_agent_0001").strip(),
            worker_lease_seconds=_integer(
                source,
                "YAYA_WORKER_LEASE_SECONDS",
                30,
                minimum=2,
                maximum=3600,
            ),
            worker_poll_ms=_integer(
                source,
                "YAYA_WORKER_POLL_MS",
                100,
                minimum=10,
                maximum=60_000,
            ),
            sandbox_wall_ms=_integer(
                source,
                "YAYA_SANDBOX_WALL_MS",
                2000,
                minimum=10,
                maximum=120_000,
            ),
            sandbox_cpu_ms=_integer(
                source,
                "YAYA_SANDBOX_CPU_MS",
                1000,
                minimum=10,
                maximum=120_000,
            ),
            sandbox_memory_bytes=_integer(
                source,
                "YAYA_SANDBOX_MEMORY_BYTES",
                67_108_864,
                minimum=1_048_576,
                maximum=4_294_967_296,
            ),
            sandbox_max_intents=_integer(
                source,
                "YAYA_SANDBOX_MAX_INTENTS",
                64,
                minimum=1,
                maximum=10_000,
            ),
            sandbox_max_output_bytes=_integer(
                source,
                "YAYA_SANDBOX_MAX_OUTPUT_BYTES",
                65_536,
                minimum=1024,
                maximum=16_777_216,
            ),
            sandbox_max_processes=_integer(
                source,
                "YAYA_SANDBOX_MAX_PROCESSES",
                1,
                minimum=1,
                maximum=128,
            ),
            sandbox_image=_required(source, "YAYA_SANDBOX_IMAGE"),
            docker_executable=source.get("YAYA_DOCKER_EXE", "docker").strip(),
            learner_worker_id=source.get(
                "YAYA_LEARNER_WORKER_ID",
                "learner_worker_0001",
            ).strip(),
            learner_worker_lease_seconds=_integer(
                source,
                "YAYA_LEARNER_WORKER_LEASE_SECONDS",
                30,
                minimum=2,
                maximum=3600,
            ),
            learner_worker_poll_ms=_integer(
                source,
                "YAYA_LEARNER_WORKER_POLL_MS",
                100,
                minimum=10,
                maximum=60_000,
            ),
        )


__all__ = ["LearnerWorkerSettings", "ProductionSettings"]
