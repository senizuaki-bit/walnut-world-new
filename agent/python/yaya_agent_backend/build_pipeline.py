"""Compatibility imports for the provider-neutral :mod:`yaya_agent_build` package.

New code must import these primitives from ``yaya_agent_build``.  This module
contains no build implementation; it preserves the historical backend import
path for downstream consumers while they migrate.
"""

from yaya_agent_build import (
    CPP20_SAFE_V1_FLAGS,
    CPP20_SAFE_V1_PROFILE,
    ArtifactIntegrityError,
    ArtifactPublicationError,
    BuildDiagnostic,
    BuildResourceLimits,
    CommandExecutionError,
    CommandOutputLimitError,
    CommandResult,
    CommandRunner,
    CommandTimeoutError,
    CommandUnavailableError,
    ContentAddressedArtifactPublisher,
    CppTestCase,
    CppTestSuite,
    DigestPinnedDockerCppBuilder,
    DockerBuildFailure,
    DockerBuildResult,
    DockerTestResult,
    PublishedArtifact,
    SourceBundleValidationError,
    SubprocessCommandRunner,
    ValidatedSourceBundle,
    ValidatedSourceFile,
    canonical_source_bundle_sha256,
    validate_source_bundle,
)

__all__ = [
    "ArtifactIntegrityError",
    "ArtifactPublicationError",
    "BuildDiagnostic",
    "BuildResourceLimits",
    "CPP20_SAFE_V1_FLAGS",
    "CPP20_SAFE_V1_PROFILE",
    "CommandExecutionError",
    "CommandOutputLimitError",
    "CommandResult",
    "CommandRunner",
    "CommandTimeoutError",
    "CommandUnavailableError",
    "ContentAddressedArtifactPublisher",
    "CppTestCase",
    "CppTestSuite",
    "DigestPinnedDockerCppBuilder",
    "DockerBuildFailure",
    "DockerBuildResult",
    "DockerTestResult",
    "PublishedArtifact",
    "SourceBundleValidationError",
    "SubprocessCommandRunner",
    "ValidatedSourceBundle",
    "ValidatedSourceFile",
    "canonical_source_bundle_sha256",
    "validate_source_bundle",
]
