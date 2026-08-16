"""Fail-fast construction of the only production-capable LLM boundary."""

from __future__ import annotations

from yaya_agent_runtime.adapters import (
    RecoverableOpenAIRelayAdapter,
    RelayHttpTransport,
    UrllibRelayHttpTransport,
)

from .provider_config import RecoverableProviderSettings


async def create_recoverable_provider(
    settings: RecoverableProviderSettings,
    *,
    transport: RelayHttpTransport | None = None,
) -> RecoverableOpenAIRelayAdapter:
    """Build and prove relay capabilities before a worker may claim jobs."""

    adapter = RecoverableOpenAIRelayAdapter(
        settings.adapter_config(),
        transport or UrllibRelayHttpTransport(max_response_bytes=settings.max_response_bytes),
    )
    await adapter.validate_capabilities()
    return adapter


__all__ = ["create_recoverable_provider"]
