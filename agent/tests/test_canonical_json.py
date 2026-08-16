from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from yaya_agent_contracts.models import (
    _canonical_json_v1,
    _freeze_mapping,
    _json_sha256,
    canonical_json_sha256,
    canonical_json_v1,
)

AGENT_ROOT = Path(__file__).resolve().parents[1]


class CanonicalJsonTests(unittest.TestCase):
    def test_python_hash_matches_the_cross_language_frozen_vector(self) -> None:
        specification = json.loads(
            (AGENT_ROOT / "contracts" / "canonical-json-v1.json").read_text(encoding="utf-8")
        )
        vector = specification["vectors"][0]
        canonical = json.dumps(
            vector["value"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assertEqual(canonical, vector["canonical_utf8"])
        self.assertEqual(
            _json_sha256(_freeze_mapping(vector["value"], "vector")),
            vector["sha256"],
        )
        self.assertEqual(canonical_json_v1(vector["value"]), vector["canonical_utf8"])
        self.assertEqual(canonical_json_sha256(vector["value"]), vector["sha256"])

    def test_key_insertion_order_does_not_change_the_hash(self) -> None:
        left = _freeze_mapping({"z": 1, "a": {"y": 2, "x": 3}}, "left")
        right = _freeze_mapping({"a": {"x": 3, "y": 2}, "z": 1}, "right")
        self.assertEqual(_json_sha256(left), _json_sha256(right))

    def test_integral_floats_and_negative_zero_are_normalized(self) -> None:
        value = _freeze_mapping({"positive": 1.0, "negative_zero": -0.0}, "value")
        self.assertEqual(
            _canonical_json_v1(value),
            '{"negative_zero":0,"positive":1}',
        )

    def test_ambiguous_or_unsafe_numbers_fail_loud(self) -> None:
        for number in (1.5, 9_007_199_254_740_992, -9_007_199_254_740_992):
            with self.subTest(number=number):
                frozen = _freeze_mapping({"number": number}, "value")
                with self.assertRaisesRegex(ValueError, "safe integer"):
                    _json_sha256(frozen)
        for number in (math.nan, math.inf, -math.inf):
            with self.subTest(number=number):
                with self.assertRaisesRegex(ValueError, "finite JSON numbers"):
                    _freeze_mapping({"number": number}, "value")

    def test_safe_integer_boundaries_are_accepted(self) -> None:
        value = _freeze_mapping(
            {
                "minimum": -9_007_199_254_740_991,
                "maximum": 9_007_199_254_740_991,
            },
            "value",
        )
        self.assertEqual(
            _canonical_json_v1(value),
            '{"maximum":9007199254740991,"minimum":-9007199254740991}',
        )

    def test_ill_formed_unicode_fails_loud(self) -> None:
        self.assertEqual(
            _canonical_json_v1(_freeze_mapping({"emoji": "🌱"}, "value")), '{"emoji":"🌱"}'
        )
        for value in ("\ud800", "\udc00", "prefix\ud800suffix"):
            with self.subTest(value=ascii(value)):
                frozen = _freeze_mapping({"value": value}, "value")
                with self.assertRaisesRegex(ValueError, "Unicode scalar"):
                    _json_sha256(frozen)
        frozen_key = _freeze_mapping({"\ud800": "invalid key"}, "value")
        with self.assertRaisesRegex(ValueError, "Unicode scalar"):
            _json_sha256(frozen_key)


if __name__ == "__main__":
    unittest.main()
