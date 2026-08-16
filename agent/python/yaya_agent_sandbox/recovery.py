"""Recovery capability for provider-neutral Sandbox adapters."""

from __future__ import annotations

from typing import Protocol

from yaya_agent_contracts import (
    OperationContext,
    Result,
    SandboxPort,
    SandboxRunRequest,
    SandboxRunResult,
)


class RecoverableSandboxPort(SandboxPort, Protocol):
    """Reconcile one exact dispatched run without dispatching it twice.

    Implementations return the durable terminal ``Result`` for the complete
    request identity, or ``None`` while no terminal outcome is safely
    observable. Identity drift and corrupt recovery state fail closed.
    """

    async def reconcile(
        self,
        request: SandboxRunRequest,
        context: OperationContext,
    ) -> Result[SandboxRunResult] | None: ...


__all__ = ["RecoverableSandboxPort"]
