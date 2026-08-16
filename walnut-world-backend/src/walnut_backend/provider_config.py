"""Strict production configuration for the recoverable Provider relay."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from yaya_agent_runtime.adapters import RecoverableOpenAIRelayConfig


@dataclass(frozen=True, slots=True)
class RecoverableProviderSettings:
    """Environment-owned relay settings with no direct Provider fallback."""

    relay_endpoint: str
    relay_api_key: str = field(repr=False)
    model: str = ""
    provider: str = ""
    response_format: Literal["json_object", "json_schema"] = "json_object"
    thinking_mode: Literal["enabled", "disabled"] | None = None
    allow_insecure_localhost: bool = False
    required_retention_seconds: int = 604_800
    max_response_bytes: int = 2_097_152
    capability_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        # Keep one validator for endpoint safety, protocol bounds, and secrets.
        self.adapter_config()

    def adapter_config(self) -> RecoverableOpenAIRelayConfig:
        return RecoverableOpenAIRelayConfig(
            relay_endpoint=self.relay_endpoint,
            api_key=self.relay_api_key,
            model=self.model,
            provider=self.provider,
            response_format=self.response_format,
            allow_insecure_localhost=self.allow_insecure_localhost,
            thinking_mode=self.thinking_mode,
            required_retention_seconds=self.required_retention_seconds,
            max_response_bytes=self.max_response_bytes,
            capability_timeout_ms=self.capability_timeout_ms,
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> RecoverableProviderSettings:
        values = os.environ if environ is None else environ
        response_format = values.get("WALNUT_LLM_RESPONSE_FORMAT", "json_object")
        if response_format not in {"json_object", "json_schema"}:
            raise ValueError("WALNUT_LLM_RESPONSE_FORMAT is not supported")
        thinking_mode = values.get("WALNUT_LLM_THINKING_MODE") or None
        if thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("WALNUT_LLM_THINKING_MODE must be enabled or disabled")
        return cls(
            relay_endpoint=_required(values, "WALNUT_LLM_RELAY_ENDPOINT"),
            relay_api_key=_required(values, "WALNUT_LLM_RELAY_API_KEY"),
            model=_required(values, "WALNUT_LLM_MODEL"),
            provider=_required(values, "WALNUT_LLM_PROVIDER"),
            response_format=cast(
                Literal["json_object", "json_schema"],
                response_format,
            ),
            thinking_mode=cast(
                Literal["enabled", "disabled"] | None,
                thinking_mode,
            ),
            allow_insecure_localhost=_boolean(
                values,
                "WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST",
                False,
            ),
            required_retention_seconds=_integer(
                values,
                "WALNUT_LLM_RELAY_REQUIRED_RETENTION_SECONDS",
                604_800,
            ),
            max_response_bytes=_integer(
                values,
                "WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES",
                2_097_152,
            ),
            capability_timeout_ms=_integer(
                values,
                "WALNUT_LLM_RELAY_CAPABILITY_TIMEOUT_MS",
                5_000,
            ),
        )


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _integer(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean")


__all__ = ["RecoverableProviderSettings"]
