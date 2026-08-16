"""Replaceable outbound adapters for the Agent Runtime."""

from .openai_compatible import (
    HttpResponse,
    OpenAICompatibleConfig,
    OpenAICompatibleLlmAdapter,
    UrllibHttpTransport,
)
from .recoverable_openai_relay import (
    RecoverableOpenAIRelayAdapter,
    RecoverableOpenAIRelayConfig,
    RelayCapabilityError,
    RelayConflictError,
    RelayDependencyUnavailable,
    RelayError,
    RelayHttpTransport,
    RelayProtocolError,
    RelayResultExpired,
    RelayTransportError,
    UrllibRelayHttpTransport,
)

__all__ = [
    "HttpResponse",
    "OpenAICompatibleConfig",
    "OpenAICompatibleLlmAdapter",
    "RecoverableOpenAIRelayAdapter",
    "RecoverableOpenAIRelayConfig",
    "RelayCapabilityError",
    "RelayConflictError",
    "RelayDependencyUnavailable",
    "RelayError",
    "RelayHttpTransport",
    "RelayProtocolError",
    "RelayResultExpired",
    "RelayTransportError",
    "UrllibHttpTransport",
    "UrllibRelayHttpTransport",
]
