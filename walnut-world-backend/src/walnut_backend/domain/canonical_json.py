"""YAYA_CANONICAL_JSON_V1 primitives shared by persistence and transport."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any


def canonical_payload(payload: Mapping[str, Any]) -> bytes:
    normalized = canonical_value(payload)
    return json.dumps(
        normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("unpaired UTF-16 surrogate")
        return value
    if isinstance(value, int):
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise ValueError("unsafe integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value != 0:
            raise ValueError("numbers must be safe integers")
        return 0
    if isinstance(value, list):
        return [canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("object key is not a string")
            normalized[canonical_value(key)] = canonical_value(nested)
        return normalized
    raise TypeError("unsupported canonical JSON value")
