"""Provider-neutral native and digest-pinned Docker Sandbox adapters.

Importing this package defines Sandbox adapters only.  It does not assemble an
HTTP server, database, migrator, worker, world engine, or model provider.
"""

from .docker import (
    DockerCppSandbox,
    SandboxOutcomeUnknownError,
    SandboxResultIntegrityError,
)
from .native import ArgumentBuilder, ProductionCppSandbox
from .recovery import RecoverableSandboxPort

__all__ = [
    "ArgumentBuilder",
    "DockerCppSandbox",
    "ProductionCppSandbox",
    "RecoverableSandboxPort",
    "SandboxOutcomeUnknownError",
    "SandboxResultIntegrityError",
]
