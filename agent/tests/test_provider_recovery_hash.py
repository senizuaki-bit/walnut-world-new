from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_contracts import (  # noqa: E402
    FrozenJsonObject,
    LlmMessage,
    LlmRequest,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import llm_recovery_sha256, llm_request_sha256  # noqa: E402
from yaya_agent_runtime.model_output import build_model_output_schema  # noqa: E402


class LlmRecoveryHashTests(unittest.TestCase):
    def test_integer_only_vector_preserves_canonical_json_v1_digest(self) -> None:
        value = {"schema_version": "1.0.0", "completion": {"temperature": 0}}

        self.assertEqual(llm_recovery_sha256(value), canonical_json_sha256(value))
        self.assertEqual(
            llm_recovery_sha256(value),
            "3935b727c8e7121b5161d5ccb672de4146a2c7481a1368e030dd45f0c94f8ed9",
        )

    def test_decimal_vector_is_fixed_and_normalizes_key_order_and_negative_zero(self) -> None:
        first = {
            "temperature": 0.2,
            "schema": {"minimum": -0.3, "multipleOf": 0.000001, "zero": -0.0},
        }
        reordered = {
            "schema": {"zero": 0.0, "multipleOf": 1e-6, "minimum": -0.30},
            "temperature": 0.20,
        }

        self.assertEqual(llm_recovery_sha256(first), llm_recovery_sha256(reordered))
        self.assertEqual(
            llm_recovery_sha256(first),
            "a441d7a87c8eed993f5ccdec63df8c1ff067157cb323abb3c120735232eaf125",
        )

    def test_non_finite_numbers_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "non-finite"):
                llm_recovery_sha256({"value": value})

    def test_non_json_and_unsafe_values_are_rejected_after_fallback(self) -> None:
        invalid = (
            {"fraction": 0.2, "unsafe": 9_007_199_254_740_992},
            {"fraction": 0.2, "surrogate": "\ud800"},
            {"fraction": 0.2, 1: "non-string-key"},
            {"fraction": 0.2, "bytes": b"not-json"},
        )
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises((TypeError, ValueError)):
                llm_recovery_sha256(cast(dict[str, object], value))

    def test_real_teaching_output_schema_and_fractional_temperature_hash(self) -> None:
        schema = build_model_output_schema(
            (),
            max_tool_calls=0,
            role="teaching_agent",
        )
        request = LlmRequest(
            messages=(LlmMessage("system", "Return strict JSON."),),
            output_schema=cast(FrozenJsonObject, schema),
            temperature=0.2,
            max_output_tokens=256,
            timeout_ms=5_000,
            versions=VersionSet(
                "1.0.0",
                "1",
                "worker-runtime-v1",
                "farm-rules-1",
                "agent-teaching-v1",
                prompt_version="prompt-v1",
                model_version="model-v1",
            ),
        )

        digest = llm_request_sha256(request)

        self.assertEqual(len(digest), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in digest))


if __name__ == "__main__":
    unittest.main()
