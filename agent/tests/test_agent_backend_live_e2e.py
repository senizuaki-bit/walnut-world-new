from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from yaya_agent_runtime.adapters import HttpResponse, UrllibHttpTransport


def _required_provider_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is required: the production E2E never substitutes a mock model provider"
        )
    return value


def _required_provider_api_key() -> str:
    direct = os.environ.get("YAYA_LLM_API_KEY", "").strip()
    key_file_raw = os.environ.get("YAYA_LLM_API_KEY_FILE", "").strip()
    if direct and key_file_raw:
        raise RuntimeError("set only one of YAYA_LLM_API_KEY and YAYA_LLM_API_KEY_FILE")
    if direct:
        return direct
    if not key_file_raw:
        raise RuntimeError(
            "YAYA_LLM_API_KEY or YAYA_LLM_API_KEY_FILE is required: "
            "the production E2E never substitutes a mock model provider"
        )
    key_file = Path(key_file_raw).expanduser().resolve()
    if not key_file.is_file():
        raise RuntimeError("YAYA_LLM_API_KEY_FILE does not identify a file")
    try:
        value = key_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("YAYA_LLM_API_KEY_FILE could not be read as UTF-8") from error
    if not value:
        raise RuntimeError("YAYA_LLM_API_KEY_FILE is empty")
    return value


def _provider_thinking_mode() -> Literal["enabled", "disabled"] | None:
    value = os.environ.get("YAYA_LLM_THINKING_MODE", "").strip().lower()
    if value == "":
        return None
    if value == "enabled":
        return "enabled"
    if value == "disabled":
        return "disabled"
    raise RuntimeError("YAYA_LLM_THINKING_MODE must be enabled or disabled")


def _required_generation_budget() -> int:
    raw = os.environ.get("YAYA_LIVE_GENERATION_BUDGET", "").strip()
    if not raw:
        raise RuntimeError("YAYA_LIVE_GENERATION_BUDGET is required before any real Provider E2E")
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError("YAYA_LIVE_GENERATION_BUDGET must be an integer") from error
    if not 1 <= value <= 64:
        raise RuntimeError("YAYA_LIVE_GENERATION_BUDGET must be between 1 and 64")
    return value


class _GenerationBudget:
    """One process-local hard ceiling shared across Provider transport restarts."""

    def __init__(self, limit: int) -> None:
        if not 1 <= limit <= 64:
            raise ValueError("generation budget must be between 1 and 64")
        self._remaining = limit
        self.used = 0

    def reserve_before_dispatch(self) -> None:
        if self._remaining <= 0:
            raise RuntimeError("LIVE_PROVIDER_GENERATION_BUDGET_EXHAUSTED")
        self._remaining -= 1
        self.used += 1


class GenerationBudgetTransport:
    """Reserve a generation before forwarding one real Provider HTTP request."""

    def __init__(self, delegate: object, budget: _GenerationBudget) -> None:
        self._delegate = delegate
        self._budget = budget

    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_ms: int,
    ) -> HttpResponse:
        self._budget.reserve_before_dispatch()
        method = getattr(self._delegate, "post_json")
        response = await method(url, headers, body, timeout_ms)
        if not isinstance(response, HttpResponse):
            raise TypeError("generation-budget delegate returned an invalid response")
        return response


def _generation_budget_guard(limit: int):
    budget = _GenerationBudget(limit)

    def build(*, max_response_bytes: int) -> GenerationBudgetTransport:
        return GenerationBudgetTransport(
            UrllibHttpTransport(max_response_bytes=max_response_bytes),
            budget,
        )

    guard = patch(
        "yaya_agent_backend.composition.UrllibHttpTransport",
        side_effect=build,
    )
    return guard, budget


class LiveProviderSettingTests(unittest.TestCase):
    def test_api_key_accepts_exactly_one_direct_or_file_source(self) -> None:
        with patch.dict(
            os.environ,
            {"YAYA_LLM_API_KEY": "direct-secret", "YAYA_LLM_API_KEY_FILE": ""},
            clear=False,
        ):
            self.assertEqual(_required_provider_api_key(), "direct-secret")

        with tempfile.TemporaryDirectory(prefix="yaya-provider-key-") as raw_root:
            key_file = Path(raw_root) / "api-key"
            key_file.write_text("file-secret\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"YAYA_LLM_API_KEY": "", "YAYA_LLM_API_KEY_FILE": str(key_file)},
                clear=False,
            ):
                self.assertEqual(_required_provider_api_key(), "file-secret")

        cases = (
            ("direct-secret", "C:/not-used", "set only one"),
            ("", "", "is required"),
        )
        for direct, key_file, expected in cases:
            with self.subTest(direct=bool(direct), key_file=bool(key_file)):
                with patch.dict(
                    os.environ,
                    {
                        "YAYA_LLM_API_KEY": direct,
                        "YAYA_LLM_API_KEY_FILE": key_file,
                    },
                    clear=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        _required_provider_api_key()

    def test_provider_settings_fail_loud_instead_of_selecting_a_fake(self) -> None:
        with patch.dict(os.environ, {"YAYA_LLM_ENDPOINT": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "YAYA_LLM_ENDPOINT is required"):
                _required_provider_setting("YAYA_LLM_ENDPOINT")
        with patch.dict(os.environ, {"YAYA_LLM_THINKING_MODE": "maybe"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "must be enabled or disabled"):
                _provider_thinking_mode()
        for value in ("", "zero", "0", "65"):
            with self.subTest(generation_budget=value):
                with patch.dict(
                    os.environ,
                    {"YAYA_LIVE_GENERATION_BUDGET": value},
                    clear=False,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "YAYA_LIVE_GENERATION_BUDGET",
                    ):
                        _required_generation_budget()


class GenerationBudgetTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_rejects_before_a_second_dispatch(self) -> None:
        class RecordingTransport:
            def __init__(self) -> None:
                self.calls = 0

            async def post_json(self, *_args) -> HttpResponse:
                self.calls += 1
                return HttpResponse(200, {}, b"{}")

        delegate = RecordingTransport()
        transport = GenerationBudgetTransport(delegate, _GenerationBudget(1))
        await transport.post_json("https://provider.invalid/v1", {}, {}, 1_000)
        with self.assertRaisesRegex(RuntimeError, "GENERATION_BUDGET_EXHAUSTED"):
            await transport.post_json("https://provider.invalid/v1", {}, {}, 1_000)
        self.assertEqual(delegate.calls, 1)


if __name__ == "__main__":
    unittest.main()
