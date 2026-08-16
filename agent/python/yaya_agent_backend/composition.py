"""Fail-closed production composition root for the Agent-turn service."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from yaya_agent_contracts import (
    ContractError,
    ErrorCategory,
    Failure,
    FrozenJsonObject,
    LlmPort,
    LlmReply,
    LlmRequest,
    OperationContext,
    Result,
    SandboxLimits,
    VersionSet,
)
from yaya_agent_runtime import (
    AgentHub,
    ContextBuilder,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RoleRouter,
    SharedAgentRuntime,
    build_default_tool_registry,
)
from yaya_agent_runtime.adapters import (
    OpenAICompatibleConfig,
    OpenAICompatibleLlmAdapter,
    UrllibHttpTransport,
)
from yaya_agent_sandbox import DockerCppSandbox

from .application import AgentTurnApplication, AgentTurnWorker
from .auth import JwtAuthenticator
from .config import LearnerWorkerSettings, ProductionSettings
from .database import PostgresDatabase
from .invocation import PostgresSkillInvocationService
from .learner_projection import LearnerProjectionWorker
from .outcome_authority import PostgresRunOutcomeAuthority
from .product_application import ProductInteractionReadApplication
from .product_repositories import PostgresProductInteractionReadRepository
from .repositories import (
    PostgresAgentTraceRepository,
    PostgresAgentTurnRepository,
    PostgresCounterexampleRepository,
    PostgresLearnerRepository,
    PostgresMessageRepository,
    PostgresRunRepository,
    PostgresSessionRepository,
    PostgresSkillRepository,
    PostgresTaskRepository,
    PostgresWorldRepository,
)
from .skill_builds import PostgresSkillBuildExecutor
from .skill_drafts import PostgresSkillDraftRepository, ProductSkillDraftApplication
from .stores import PostgresLearnerStore
from .student_skill_chain import StudentSkillChainApplication, StudentSkillChainWorker
from .wire import ContractSchemaValidator
from .world import WateringWorldEngine
from .world_uow import PostgresWorldUnitOfWork


class ExplicitFallbackLlmAdapter(LlmPort):
    """A declared no-provider mode that enters Runtime's deterministic fallback."""

    async def generate(
        self,
        request: LlmRequest,
        context: OperationContext,
    ) -> Result[LlmReply]:
        del request, context
        return Failure(
            ContractError(
                code="DEPENDENCY_UNAVAILABLE",
                category=ErrorCategory.DEPENDENCY,
                retryable=True,
                user_message_key="dependency.temporarily_unavailable",
                stage="MODEL_PROVIDER",
                message="Model provider is disabled by explicit production configuration.",
                details=cast(FrozenJsonObject, {"reason": "EXPLICIT_FALLBACK_MODE"}),
            )
        )


@dataclass(frozen=True, slots=True)
class _ContractReleaseBaseline:
    label: str
    lock_relative: str
    lock_bytes: int
    lock_sha256: str
    package_version: str
    git_release: str
    baseline_commit: str | None
    manifest_bytes: int
    manifest_sha256: str
    file_count: int
    manifest_entry_count: int
    base_entries_sha256: str
    additive_paths: tuple[str, ...] = ()
    release_status: str | None = None


@dataclass(frozen=True, slots=True)
class _VerifiedReleaseLock:
    spec: _ContractReleaseBaseline
    inventory: tuple[tuple[str, int, str], ...] | None


_CONTRACT_RELEASE_BASELINES = (
    _ContractReleaseBaseline(
        label="v0.3",
        lock_relative="contracts/releases/agent-contracts-v0.3.lock.json",
        lock_bytes=767,
        lock_sha256="c58a091f8f4c90f0d317448e846db79ffffafe64790f027a97a14f38e437ed7a",
        package_version="0.3.0",
        git_release="refs/tags/agent-contracts-v0.3.0",
        baseline_commit="7841120",
        manifest_bytes=25_384,
        manifest_sha256="f1898f70642c2387965ca8b15c32df611eb92cd69c3f42de61fa7c6fb242917e",
        file_count=135,
        manifest_entry_count=134,
        base_entries_sha256="c4f10d841d72d6d6888aa95343845a89d5688909aa42df92491bcec589dddbf9",
        additive_paths=(
            "contracts/examples/game-student-bootstrap-v2.json",
            "contracts/openapi/student-bootstrap-v2.openapi.json",
            "contracts/schemas/game/student-bootstrap-v2.schema.json",
        ),
    ),
    _ContractReleaseBaseline(
        label="v0.4",
        lock_relative="contracts/releases/agent-contracts-v0.4.lock.json",
        lock_bytes=26_534,
        lock_sha256="423b2a0c11bc1a9760306ef10172cb827b89de8002b97575874105d82eaae544",
        package_version="0.4.0",
        git_release="refs/tags/agent-contracts-v0.4.0",
        baseline_commit="0494c0f8ef6eb505e43db84c0249b046be35c589",
        manifest_bytes=26_127,
        manifest_sha256="b62a6152f1f2fd87d1941beecd3a1d47089811e91b067a8b225ff0d7a5ce72b9",
        file_count=139,
        manifest_entry_count=138,
        base_entries_sha256="7d51c31e4798d5495f3ef3fdcf5d96fe3a7f4a0739da92b41905f52ee6c61062",
    ),
    _ContractReleaseBaseline(
        label="v0.5",
        lock_relative="contracts/releases/agent-contracts-v0.5.lock.json",
        lock_bytes=27_509,
        lock_sha256="63453cad85c96e3918f99896ca0e4088877a03a4a21ea9338e5536cdec0a7bba",
        package_version="0.5.0",
        git_release="refs/tags/agent-contracts-v0.5.0",
        baseline_commit=None,
        manifest_bytes=27_087,
        manifest_sha256="e90eed36e7e9c003e033884e05f19d858b0ca0b44f88660e11c8e4d7fa8a6c8b",
        file_count=144,
        manifest_entry_count=143,
        base_entries_sha256="fa1d18988cb61cd4370a22b5396778b66d1f54866e30144392c06e9a9ee062b1",
        release_status="WORKTREE_CANDIDATE_NOT_TAGGED",
    ),
)
_CONTRACT_ENTRY_SHA = re.compile(r"[0-9a-f]{64}")
_CONTRACT_MANIFEST_FIELDS = {
    "schema_version",
    "package_name",
    "package_version",
    "git_release",
    "hash_algorithm",
    "files",
}
_BASELINE_LOCK_FIELDS = {
    "schema_version",
    "package_name",
    "package_version",
    "git_release",
    "baseline_commit",
    "manifest_path",
    "manifest_bytes",
    "manifest_sha256",
    "file_count",
    "manifest_entry_count",
    "base_entries_digest_format",
    "base_entries_sha256",
}


def _parse_contract_file_inventory(
    value: object,
    *,
    label: str,
) -> tuple[tuple[str, int, str], ...]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{label} has no file inventory")
    seen: set[str] = set()
    entries: list[tuple[str, int, str]] = []
    for raw_entry in cast(list[object], value):
        if not isinstance(raw_entry, Mapping):
            raise RuntimeError(f"{label} contains a non-object file entry")
        entry = cast(Mapping[str, object], raw_entry)
        if set(entry) != {"path", "bytes", "sha256"}:
            raise RuntimeError(f"{label} file entry is not closed")
        relative = entry.get("path")
        expected_bytes = entry.get("bytes")
        expected_sha = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or relative in seen
            or not relative.startswith("contracts/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or not isinstance(expected_sha, str)
            or _CONTRACT_ENTRY_SHA.fullmatch(expected_sha) is None
        ):
            raise RuntimeError(f"{label} contains an invalid file identity")
        seen.add(relative)
        entries.append((relative, expected_bytes, expected_sha))
    if entries != sorted(entries, key=lambda item: item[0]):
        raise RuntimeError(f"{label} file inventory is not sorted")
    return tuple(entries)


def _contract_entries_sha256(entries: tuple[tuple[str, int, str], ...]) -> str:
    encoded = json.dumps(
        [list(entry) for entry in entries],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_release_lock(
    repository_root: Path,
    current_entries: Mapping[str, tuple[str, int, str]],
    spec: _ContractReleaseBaseline,
) -> _VerifiedReleaseLock:
    current_identity = current_entries.get(spec.lock_relative)
    expected_identity = (spec.lock_relative, spec.lock_bytes, spec.lock_sha256)
    if current_identity != expected_identity:
        raise RuntimeError(f"{spec.label} baseline lock bytes drifted")
    lock_path = repository_root / spec.lock_relative
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            raise OSError("baseline lock is not a regular file")
        raw_lock = lock_path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"{spec.label} baseline lock is unavailable or invalid") from error
    if len(raw_lock) != spec.lock_bytes or hashlib.sha256(raw_lock).hexdigest() != spec.lock_sha256:
        raise RuntimeError(f"{spec.label} baseline lock bytes drifted")
    try:
        lock_value = json.loads(raw_lock.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{spec.label} baseline lock is unavailable or invalid") from error
    if not isinstance(lock_value, Mapping):
        raise RuntimeError(f"{spec.label} baseline lock root must be an object")
    lock = cast(Mapping[str, object], lock_value)
    inventory_field = "additive_paths" if spec.additive_paths else "files"
    expected_fields = {*_BASELINE_LOCK_FIELDS, inventory_field}
    if spec.release_status is not None:
        expected_fields.add("release_status")
    if set(lock) != expected_fields:
        raise RuntimeError(f"{spec.label} baseline lock is not closed")
    if (
        lock.get("schema_version") != "1.0.0"
        or lock.get("package_name") != "@yaya/agent-contracts"
        or lock.get("package_version") != spec.package_version
        or lock.get("git_release") != spec.git_release
        or lock.get("baseline_commit") != spec.baseline_commit
        or lock.get("manifest_path") != "contracts/manifest.json"
        or lock.get("manifest_bytes") != spec.manifest_bytes
        or lock.get("manifest_sha256") != spec.manifest_sha256
        or lock.get("file_count") != spec.file_count
        or lock.get("manifest_entry_count") != spec.manifest_entry_count
        or lock.get("base_entries_digest_format") != "json-array[path,bytes,sha256]"
        or lock.get("base_entries_sha256") != spec.base_entries_sha256
        or (spec.release_status is not None and lock.get("release_status") != spec.release_status)
    ):
        raise RuntimeError(f"{spec.label} baseline lock identity drifted")
    if spec.additive_paths:
        additive_value = lock.get("additive_paths")
        if (
            not isinstance(additive_value, list)
            or tuple(cast(list[object], additive_value)) != spec.additive_paths
        ):
            raise RuntimeError(f"{spec.label} baseline lock additive paths drifted")
        return _VerifiedReleaseLock(spec=spec, inventory=None)

    inventory = _parse_contract_file_inventory(
        lock.get("files"),
        label=f"{spec.label} baseline lock",
    )
    if len(inventory) != spec.manifest_entry_count or len(inventory) + 1 != spec.file_count:
        raise RuntimeError(f"{spec.label} frozen manifest entry count drifted")
    if _contract_entries_sha256(inventory) != spec.base_entries_sha256:
        raise RuntimeError(f"{spec.label} frozen manifest entries drifted")
    return _VerifiedReleaseLock(spec=spec, inventory=inventory)


def verify_contract_manifest(contracts_root: Path) -> None:
    """Verify every immutable contract byte before any production adapter starts."""

    root = contracts_root.expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise OSError("contract manifest is not a regular file")
        raw_manifest = manifest_path.read_bytes()
        manifest_value = json.loads(raw_manifest.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("contract manifest is unavailable or invalid") from error
    if not isinstance(manifest_value, Mapping):
        raise RuntimeError("contract manifest root must be an object")
    manifest = cast(Mapping[str, object], manifest_value)
    if set(manifest) != _CONTRACT_MANIFEST_FIELDS:
        raise RuntimeError("contract manifest is not closed")
    package_version = manifest.get("package_version")
    if (
        manifest.get("schema_version") != "1.0.0"
        or manifest.get("package_name") != "@yaya/agent-contracts"
        or not isinstance(package_version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", package_version) is None
        or manifest.get("git_release") != f"refs/tags/agent-contracts-v{package_version}"
        or manifest.get("hash_algorithm") != "sha256"
    ):
        raise RuntimeError("contract manifest release identity is invalid")
    manifest_entries = _parse_contract_file_inventory(
        manifest.get("files"),
        label="contract manifest",
    )
    repository_root = root.parent
    for relative, expected_bytes, expected_sha in manifest_entries:
        target = repository_root / relative
        try:
            if target.is_symlink() or not target.is_file():
                raise OSError("contract entry is not a regular file")
            resolved_target = target.resolve(strict=True)
            resolved_target.relative_to(root)
            payload = resolved_target.read_bytes()
        except (OSError, ValueError) as error:
            raise RuntimeError(f"contract file is unavailable: {relative}") from error
        if len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != expected_sha:
            raise RuntimeError(f"contract file hash drifted: {relative}")

    try:
        actual_inventory: list[str] = []
        for candidate in root.rglob("*"):
            relative_candidate = candidate.relative_to(repository_root).as_posix()
            if candidate.is_symlink():
                raise RuntimeError(
                    f"contract inventory contains a symbolic link: {relative_candidate}"
                )
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise RuntimeError(f"contract inventory contains a non-file: {relative_candidate}")
            if candidate != manifest_path:
                actual_inventory.append(relative_candidate)
    except OSError as error:
        raise RuntimeError("contract inventory is unavailable") from error
    expected_inventory = [entry[0] for entry in manifest_entries]
    if sorted(actual_inventory) != expected_inventory:
        raise RuntimeError("current manifest inventory drifted")

    current_entries = {entry[0]: entry for entry in manifest_entries}
    verified_locks = {
        spec.label: _read_release_lock(repository_root, current_entries, spec)
        for spec in _CONTRACT_RELEASE_BASELINES
    }
    v03 = verified_locks["v0.3"]
    v04 = verified_locks["v0.4"]
    if v04.inventory is None:
        raise RuntimeError("v0.4 baseline lock has no frozen inventory")
    if any(path not in current_entries for path in v03.spec.additive_paths):
        raise RuntimeError("v0.4 additive contract is absent from the manifest")
    excluded = {v03.spec.lock_relative, *v03.spec.additive_paths}
    v03_entries = tuple(entry for entry in v04.inventory if entry[0] not in excluded)
    if len(v03_entries) != v03.spec.manifest_entry_count:
        raise RuntimeError("v0.3 frozen manifest entry count drifted")
    if _contract_entries_sha256(v03_entries) != v03.spec.base_entries_sha256:
        raise RuntimeError("v0.3 frozen manifest entries drifted")
    if any(current_entries.get(entry[0]) != entry for entry in v03_entries):
        raise RuntimeError("v0.3 frozen manifest entries drifted")

    for label in ("v0.4", "v0.5"):
        verified = verified_locks[label]
        if verified.inventory is None:
            raise RuntimeError(f"{label} baseline lock has no frozen inventory")
        for frozen_entry in verified.inventory:
            if current_entries.get(frozen_entry[0]) != frozen_entry:
                raise RuntimeError(f"{label} frozen file drifted: {frozen_entry[0]}")


def production_versions(settings: ProductionSettings) -> VersionSet:
    return VersionSet(
        api_version="1.0.0",
        event_version="1",
        policy_version="agent-policy-v1",
        world_rules_version="farm-rules-1",
        teaching_spec_version="agent-teaching-v1",
        compiler_version="gcc-cpp20-container",
        sandbox_image_digest=settings.sandbox_image,
        test_suite_version="agent-turn-production-v1",
        prompt_version="agent-runtime-v2",
        model_version=settings.llm_model,
    )


@dataclass(frozen=True, slots=True)
class ProductionComposition:
    settings: ProductionSettings
    database: PostgresDatabase
    authenticator: JwtAuthenticator
    validator: ContractSchemaValidator
    application: AgentTurnApplication
    product_repository: PostgresProductInteractionReadRepository
    product_application: ProductInteractionReadApplication
    draft_repository: PostgresSkillDraftRepository
    draft_application: ProductSkillDraftApplication
    student_chain_application: StudentSkillChainApplication
    student_chain_worker: StudentSkillChainWorker
    build_executor: PostgresSkillBuildExecutor
    worker: AgentTurnWorker
    learner_store: PostgresLearnerStore
    learner_worker: LearnerProjectionWorker
    hub: AgentHub
    runtime: SharedAgentRuntime
    sandbox: DockerCppSandbox
    world_uow: PostgresWorldUnitOfWork
    invocations: PostgresSkillInvocationService
    turns: PostgresAgentTurnRepository


@dataclass(frozen=True, slots=True)
class LearnerWorkerComposition:
    """Minimal process graph for independently deployed learner projection."""

    settings: LearnerWorkerSettings
    database: PostgresDatabase
    learner_store: PostgresLearnerStore
    learner_worker: LearnerProjectionWorker


def _build_learner_worker(
    database: PostgresDatabase,
    *,
    worker_id: str,
    lease_seconds: int,
    poll_ms: int,
) -> tuple[PostgresLearnerStore, LearnerProjectionWorker]:
    learner_store = PostgresLearnerStore(database)
    learner_worker = LearnerProjectionWorker(
        database=database,
        learner=learner_store,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        poll_ms=poll_ms,
    )
    return learner_store, learner_worker


async def create_learner_worker_composition(
    settings: LearnerWorkerSettings,
    *,
    migrate: bool = True,
) -> LearnerWorkerComposition:
    """Build only PostgreSQL learner projection dependencies."""

    verify_contract_manifest(settings.contracts_root)
    database = PostgresDatabase(settings.database_dsn)
    if migrate:
        await database.migrate()
    learner_store, learner_worker = _build_learner_worker(
        database,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        poll_ms=settings.poll_ms,
    )
    return LearnerWorkerComposition(
        settings=settings,
        database=database,
        learner_store=learner_store,
        learner_worker=learner_worker,
    )


async def create_production_composition(
    settings: ProductionSettings,
    *,
    migrate: bool = True,
) -> ProductionComposition:
    """Instantiate the complete provider-neutral runtime and production adapters."""

    verify_contract_manifest(settings.contracts_root)
    database = PostgresDatabase(settings.database_dsn)
    if migrate:
        await database.migrate()
    # DockerCppSandbox is deliberately the only production Sandbox.  Its
    # constructor verifies the exact pinned Linux image and never falls back to
    # the native host adapter.
    sandbox_result_root = settings.artifact_root / ".sandbox-results"
    sandbox_result_root.mkdir(mode=0o700, exist_ok=True)
    sandbox = DockerCppSandbox(
        settings.artifact_root,
        image=settings.sandbox_image,
        result_root=sandbox_result_root,
        docker_executable=settings.docker_executable,
    )
    versions = production_versions(settings)
    limits = SandboxLimits(
        cpu_ms=settings.sandbox_cpu_ms,
        wall_ms=settings.sandbox_wall_ms,
        memory_bytes=settings.sandbox_memory_bytes,
        max_intents=settings.sandbox_max_intents,
        max_output_bytes=settings.sandbox_max_output_bytes,
        max_processes=settings.sandbox_max_processes,
        network_access=False,
    )
    world_engine = WateringWorldEngine(supported_rules_version=versions.world_rules_version)
    world_uow = PostgresWorldUnitOfWork(database, world_engine)
    invocations = PostgresSkillInvocationService(
        database=database,
        sandbox=sandbox,
        world_engine=world_engine,
        world_uow=world_uow,
        limits=limits,
        versions=versions,
        contracts_root=settings.contracts_root,
    )
    trace = PostgresAgentTraceRepository(database)
    role_configs = PackagedRoleConfigProvider.load()
    llm: LlmPort
    if settings.llm_mode == "provider":
        if settings.llm_endpoint is None or settings.llm_api_key is None:
            raise RuntimeError("provider mode lost its validated endpoint or API key")
        llm = OpenAICompatibleLlmAdapter(
            OpenAICompatibleConfig(
                endpoint=settings.llm_endpoint,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                provider=settings.llm_provider,
                response_format=settings.llm_response_format,
                thinking_mode=settings.llm_thinking_mode,
                allow_insecure_localhost=settings.allow_insecure_llm_localhost,
            ),
            UrllibHttpTransport(max_response_bytes=settings.llm_max_response_bytes),
        )
    else:
        llm = ExplicitFallbackLlmAdapter()
    contexts = ContextBuilder(
        tasks=PostgresTaskRepository(database),
        sessions=PostgresSessionRepository(database),
        skills=PostgresSkillRepository(database),
        runs=PostgresRunRepository(database),
        counterexamples=PostgresCounterexampleRepository(database),
        learners=PostgresLearnerRepository(database),
        messages=PostgresMessageRepository(database),
        worlds=PostgresWorldRepository(database),
        role_configs=role_configs,
        teaching_spec_version=versions.teaching_spec_version,
    )
    tools = build_default_tool_registry(trace, invocations)
    runtime = SharedAgentRuntime(
        llm=llm,
        role_configs=role_configs,
        tools=tools,
        prompts=PromptBuilder(),
        trace=trace,
        versions=versions,
        clock=lambda: datetime.now(UTC),
    )
    root_budget_ms = runtime.execution_budget_ms("xiaohutao")
    final_budget_ms = max(
        runtime.execution_budget_ms("teaching_agent"),
        runtime.execution_budget_ms("bug_agent"),
        runtime.execution_budget_ms("book_agent"),
    )
    turns = PostgresAgentTurnRepository(
        database,
        settings.contracts_root,
        claim_ttl_ms=max(root_budget_ms, final_budget_ms) + 30_000,
        internalize_root_execution=True,
    )
    hub = AgentHub(
        router=RoleRouter(),
        contexts=contexts,
        runtime=runtime,
        turns=turns,
        invocations=invocations,
    )
    validator = ContractSchemaValidator(settings.contracts_root)
    outcomes = PostgresRunOutcomeAuthority(database, validator)
    application = AgentTurnApplication(database, settings.contracts_root, versions)
    product_repository = PostgresProductInteractionReadRepository(
        database,
        validator,
        require_internal_root=True,
    )
    product_application = ProductInteractionReadApplication(product_repository, validator)
    draft_repository = PostgresSkillDraftRepository(database, validator)
    draft_application = ProductSkillDraftApplication(draft_repository, validator)
    student_chain_application = StudentSkillChainApplication(
        database,
        validator,
        versions,
        artifact_root=settings.artifact_root,
    )
    build_workspace_root = settings.artifact_root / ".build-workspaces"
    build_workspace_root.mkdir(mode=0o700, exist_ok=True)
    build_executor = PostgresSkillBuildExecutor(
        database=database,
        validator=validator,
        artifact_root=settings.artifact_root,
        workspace_root=build_workspace_root,
        runtime_image=settings.sandbox_image,
        docker_executable=settings.docker_executable,
    )
    control_worker_id = (
        "control_" + hashlib.sha256(settings.worker_id.encode("utf-8")).hexdigest()[:24]
    )
    student_chain_worker = StudentSkillChainWorker(
        database=database,
        application=student_chain_application,
        validator=validator,
        worker_id=control_worker_id,
        artifact_root=settings.artifact_root,
        lease_seconds=settings.worker_lease_seconds,
        poll_ms=settings.worker_poll_ms,
        build_executor=build_executor,
    )
    worker = AgentTurnWorker(
        database=database,
        hub=hub,
        validator=validator,
        worker_id=settings.worker_id,
        configured_lease_seconds=settings.worker_lease_seconds,
        poll_ms=settings.worker_poll_ms,
        runtime_budget_ms=root_budget_ms + final_budget_ms + 15_000,
        outcome_authority=outcomes,
    )
    learner_store, learner_worker = _build_learner_worker(
        database,
        worker_id=settings.learner_worker_id,
        lease_seconds=settings.learner_worker_lease_seconds,
        poll_ms=settings.learner_worker_poll_ms,
    )
    authenticator = JwtAuthenticator(
        hmac_secret=settings.auth_hmac_secret,
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
    )
    return ProductionComposition(
        settings=settings,
        database=database,
        authenticator=authenticator,
        validator=validator,
        application=application,
        product_repository=product_repository,
        product_application=product_application,
        draft_repository=draft_repository,
        draft_application=draft_application,
        student_chain_application=student_chain_application,
        student_chain_worker=student_chain_worker,
        build_executor=build_executor,
        worker=worker,
        learner_store=learner_store,
        learner_worker=learner_worker,
        hub=hub,
        runtime=runtime,
        sandbox=sandbox,
        world_uow=world_uow,
        invocations=invocations,
        turns=turns,
    )


__all__ = [
    "ExplicitFallbackLlmAdapter",
    "LearnerWorkerComposition",
    "ProductionComposition",
    "create_learner_worker_composition",
    "create_production_composition",
    "production_versions",
    "verify_contract_manifest",
]
