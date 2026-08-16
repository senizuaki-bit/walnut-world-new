"""Production entrypoint for the backend-owned durable workflow worker."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path

from yaya_agent_contracts import SandboxLimits, VersionSet
from yaya_agent_sandbox import DockerCppSandbox

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.workflow_jobs import PostgresWorkflowJobStore
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.image_reference import require_digest_pinned_image
from walnut_backend.provider_config import RecoverableProviderSettings
from walnut_backend.provider_wiring import create_recoverable_provider
from walnut_backend.workers.build_worker import BuildWorkflowHandler
from walnut_backend.workers.control_worker import ControlWorkflowHandler
from walnut_backend.workers.turn_worker import TurnWorkflowHandler
from walnut_backend.workers.workflow_worker import WorkflowWorker


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_url: str
    tenant_id: str
    worker_id: str
    workspace_root: Path
    artifact_root: Path
    sandbox_temp_root: Path
    sandbox_result_root: Path
    docker_executable: str
    sandbox_image: str
    sandbox_image_digest: str
    provider_settings: RecoverableProviderSettings = field(repr=False)
    prompt_version: str = ""
    teaching_spec_version: str = ""
    world_rules_version: str = ""
    world_content_version: str = ""
    world_max_actions: int = 8
    world_min_x: int = 0
    world_max_x: int = 31
    world_min_y: int = 0
    world_max_y: int = 31
    world_harvest_growth_stage: int = 2
    world_success_score: int = 1
    sandbox_cpu_ms: int = 1_000
    sandbox_wall_ms: int = 2_000
    sandbox_memory_bytes: int = 67_108_864
    sandbox_max_intents: int = 32
    sandbox_max_output_bytes: int = 1_048_576
    sandbox_max_processes: int = 16
    world_presentation_enabled: bool = False
    skill_patch_enabled: bool = False
    lease_seconds: int = 900
    idle_poll_seconds: float = 0.25

    def __post_init__(self) -> None:
        for name in (
            "database_url",
            "tenant_id",
            "worker_id",
            "docker_executable",
            "sandbox_image",
            "prompt_version",
            "teaching_spec_version",
            "world_rules_version",
            "world_content_version",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must be explicitly configured")
        if not isinstance(self.provider_settings, RecoverableProviderSettings):
            raise TypeError("provider_settings must require a recoverable relay")
        if not isinstance(self.world_presentation_enabled, bool) or not isinstance(
            self.skill_patch_enabled, bool
        ):
            raise TypeError("INT2 feature flags must be boolean")
        if self.skill_patch_enabled and not self.world_presentation_enabled:
            raise ValueError(
                "Skill Patch requires the authoritative World presentation milestone"
            )
        _, validated_digest = require_digest_pinned_image(
            self.sandbox_image, "WALNUT_SANDBOX_IMAGE"
        )
        if validated_digest != self.sandbox_image_digest:
            raise ValueError("Sandbox image and digest authority differ")
        if not 30 <= self.lease_seconds <= 3600:
            raise ValueError("worker lease_seconds must be between 30 and 3600")
        if not 0.01 <= self.idle_poll_seconds <= 60:
            raise ValueError("worker idle_poll_seconds must be between 0.01 and 60")

    @classmethod
    def from_env(cls) -> WorkerSettings:
        image = _required("WALNUT_SANDBOX_IMAGE")
        _, digest = require_digest_pinned_image(image, "WALNUT_SANDBOX_IMAGE")
        runtime_root = Path(_required("WALNUT_RUNTIME_ROOT")).expanduser().resolve()
        return cls(
            database_url=_required("WALNUT_DATABASE_URL"),
            tenant_id=_required("WALNUT_TENANT_ID"),
            worker_id=os.getenv("WALNUT_WORKER_ID", "walnut-workflow-worker-1"),
            workspace_root=runtime_root / "build-workspaces",
            artifact_root=runtime_root / "artifacts",
            sandbox_temp_root=runtime_root / "sandbox-temp",
            sandbox_result_root=runtime_root / "sandbox-results",
            docker_executable=os.getenv("WALNUT_DOCKER_EXECUTABLE", "docker"),
            sandbox_image=image,
            sandbox_image_digest=digest,
            provider_settings=RecoverableProviderSettings.from_env(),
            prompt_version=_required("WALNUT_PROMPT_VERSION"),
            teaching_spec_version=_required("WALNUT_TEACHING_SPEC_VERSION"),
            world_rules_version=_required("WALNUT_WORLD_RULES_VERSION"),
            world_content_version=_required("WALNUT_WORLD_CONTENT_VERSION"),
            world_max_actions=_integer("WALNUT_WORLD_MAX_ACTIONS", 8),
            world_min_x=_integer("WALNUT_WORLD_MIN_X", 0),
            world_max_x=_integer("WALNUT_WORLD_MAX_X", 31),
            world_min_y=_integer("WALNUT_WORLD_MIN_Y", 0),
            world_max_y=_integer("WALNUT_WORLD_MAX_Y", 31),
            world_harvest_growth_stage=_integer("WALNUT_WORLD_HARVEST_GROWTH_STAGE", 2),
            world_success_score=_integer("WALNUT_WORLD_SUCCESS_SCORE", 1),
            sandbox_cpu_ms=_integer("WALNUT_SANDBOX_CPU_MS", 1_000),
            sandbox_wall_ms=_integer("WALNUT_SANDBOX_WALL_MS", 2_000),
            sandbox_memory_bytes=_integer("WALNUT_SANDBOX_MEMORY_BYTES", 67_108_864),
            sandbox_max_intents=_integer("WALNUT_SANDBOX_MAX_INTENTS", 32),
            sandbox_max_output_bytes=_integer("WALNUT_SANDBOX_MAX_OUTPUT_BYTES", 1_048_576),
            sandbox_max_processes=_integer("WALNUT_SANDBOX_MAX_PROCESSES", 16),
            world_presentation_enabled=_boolean_flag(
                "WALNUT_ENABLE_WORLD_PRESENTATION", False
            ),
            skill_patch_enabled=_boolean_flag("WALNUT_ENABLE_SKILL_PATCH", False),
            lease_seconds=_integer("WALNUT_WORKER_LEASE_SECONDS", 900),
            idle_poll_seconds=float(os.getenv("WALNUT_WORKER_IDLE_POLL_SECONDS", "0.25")),
        )


async def run_worker(settings: WorkerSettings) -> None:
    # A worker must prove the relay's atomic PUT/linearizable GET contract
    # before it creates local state, connects to PostgreSQL, or claims a Job.
    provider = await create_recoverable_provider(settings.provider_settings)
    for path in (
        settings.workspace_root,
        settings.artifact_root,
        settings.sandbox_temp_root,
        settings.sandbox_result_root,
    ):
        path.mkdir(parents=True, exist_ok=True)

    sessions = create_session_factory(settings.database_url)
    commands = PostgresCommandStore(sessions)
    jobs = PostgresWorkflowJobStore(sessions)
    rules = WorldRules(
        content_version=settings.world_content_version,
        max_actions=settings.world_max_actions,
        min_x=settings.world_min_x,
        max_x=settings.world_max_x,
        min_y=settings.world_min_y,
        max_y=settings.world_max_y,
        harvest_growth_stage=settings.world_harvest_growth_stage,
        success_score=settings.world_success_score,
    )
    versions = VersionSet(
        api_version="1.0.0",
        event_version="1",
        policy_version="worker-runtime-v1",
        world_rules_version=settings.world_rules_version,
        teaching_spec_version=settings.teaching_spec_version,
        sandbox_image_digest=settings.sandbox_image_digest,
        prompt_version=settings.prompt_version,
        model_version=settings.provider_settings.model,
    )
    sandbox = DockerCppSandbox(
        settings.artifact_root,
        image=settings.sandbox_image,
        result_root=settings.sandbox_result_root,
        docker_executable=settings.docker_executable,
        temp_root=settings.sandbox_temp_root,
    )
    limits = SandboxLimits(
        cpu_ms=settings.sandbox_cpu_ms,
        wall_ms=settings.sandbox_wall_ms,
        memory_bytes=settings.sandbox_memory_bytes,
        max_intents=settings.sandbox_max_intents,
        max_output_bytes=settings.sandbox_max_output_bytes,
        max_processes=settings.sandbox_max_processes,
        network_access=False,
    )
    handlers = (
        ControlWorkflowHandler(
            sessions,
            commands,
            jobs,
            lease_seconds=min(300, settings.lease_seconds),
        ),
        BuildWorkflowHandler(
            session_factory=sessions,
            command_store=commands,
            workflow_jobs=jobs,
            workspace_root=settings.workspace_root,
            artifact_root=settings.artifact_root,
            docker_executable=settings.docker_executable,
            lease_seconds=settings.lease_seconds,
        ),
        TurnWorkflowHandler(
            session_factory=sessions,
            commands=commands,
            jobs=jobs,
            provider=provider,
            sandbox=sandbox,
            limits=limits,
            versions=versions,
            rules_by_version={settings.world_rules_version: rules},
            provider_name=settings.provider_settings.provider,
            model_version=settings.provider_settings.model,
            prompt_version=settings.prompt_version,
            sandbox_image_digest=settings.sandbox_image_digest,
            skill_patch_enabled=settings.skill_patch_enabled,
            lease_seconds=min(600, settings.lease_seconds),
        ),
    )
    worker = WorkflowWorker(
        session_factory=sessions,
        jobs=jobs,
        commands=commands,
        handlers=handlers,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await worker.run_forever(
            settings.tenant_id,
            stop=stop,
            idle_poll_seconds=settings.idle_poll_seconds,
        )
    finally:
        await sessions.kw["bind"].dispose()


def main() -> None:
    asyncio.run(run_worker(WorkerSettings.from_env()))


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _boolean_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


if __name__ == "__main__":
    main()


__all__ = ["WorkerSettings", "run_worker"]
