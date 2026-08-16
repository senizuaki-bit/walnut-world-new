"""Closed internal JSON codec for immutable contract/runtime dataclasses.

Database JSONB is not a second public DTO surface.  Tagged values are decoded
only through a fixed in-process registry, so persisted records can be rebuilt
without pickle, ORM models or reflective imports.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from math import isfinite
from typing import cast

import yaya_agent_contracts.models as contract_models
import yaya_agent_runtime.domain as runtime_models
import yaya_agent_runtime.learner_projection_policy as learner_policy_models
import yaya_agent_runtime.pedagogy_policy as pedagogy_models
from yaya_agent_contracts import canonical_json_sha256


def _registries() -> tuple[dict[str, type[object]], dict[str, type[Enum]]]:
    dataclasses: dict[str, type[object]] = {}
    enums: dict[str, type[Enum]] = {}
    for module in (
        contract_models,
        runtime_models,
        learner_policy_models,
        pedagogy_models,
    ):
        for name, value in inspect.getmembers(module, inspect.isclass):
            if value.__module__ != module.__name__:
                continue
            if is_dataclass(value):
                dataclasses[name] = cast(type[object], value)
            if issubclass(value, Enum):
                enums[name] = value
    return dataclasses, enums


_DATACLASSES, _ENUMS = _registries()


def encode(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": type(value).__name__,
            "$fields": {
                item.name: encode(getattr(value, item.name)) for item in fields(value) if item.init
            },
        }
    if isinstance(value, Enum):
        return {"$enum": type(value).__name__, "$value": value.value}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat().replace("+00:00", "Z")}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): encode(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [encode(item) for item in sequence]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot encode internal value of type {type(value).__name__}")


def decode(value: object) -> object:
    if isinstance(value, list):
        items = cast(list[object], value)
        return [decode(item) for item in items]
    if not isinstance(value, Mapping):
        return value
    mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise ValueError("internal JSON object keys must be strings")
    typed_mapping = {cast(str, key): item for key, item in mapping.items()}
    keys = set(typed_mapping)
    if keys == {"$datetime"}:
        raw = typed_mapping["$datetime"]
        if not isinstance(raw, str):
            raise ValueError("invalid tagged datetime")
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if keys == {"$enum", "$value"}:
        name = typed_mapping["$enum"]
        if not isinstance(name, str) or name not in _ENUMS:
            raise ValueError("unknown tagged enum")
        return _ENUMS[name](typed_mapping["$value"])
    if keys == {"$type", "$fields"}:
        name = typed_mapping["$type"]
        raw_fields = typed_mapping["$fields"]
        if not isinstance(name, str) or name not in _DATACLASSES:
            raise ValueError("unknown tagged dataclass")
        if not isinstance(raw_fields, Mapping):
            raise ValueError("invalid tagged dataclass fields")
        fields_mapping = cast(Mapping[object, object], raw_fields)
        decoded_fields = {str(key): decode(item) for key, item in fields_mapping.items()}
        return _DATACLASSES[name](**decoded_fields)
    if any(str(key).startswith("$") for key in keys):
        raise ValueError("unknown internal codec tag")
    return {key: decode(item) for key, item in typed_mapping.items()}


def decode_as[T](value: object, expected: type[T]) -> T:
    decoded = decode(value)
    if not isinstance(decoded, expected):
        raise TypeError(
            f"persisted value is {type(decoded).__name__}; expected {expected.__name__}"
        )
    return decoded


def plain(value: object) -> object:
    """Convert typed values to ordinary transport-friendly JSON values."""

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: plain(getattr(value, item.name)) for item in fields(value) if item.init}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): plain(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [plain(item) for item in sequence]
    return value


def _hash_safe_json(value: object, field_name: str = "value") -> object:
    """Tag finite JSON floats before applying integer-only canonical JSON v1."""

    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite float")
        decimal = Decimal(str(value))
        normalized = format(decimal, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        if normalized in {"-0", ""}:
            normalized = "0"
        return {"$decimal_v1": normalized}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in mapping):
            raise ValueError(f"{field_name} contains a non-string object key")
        return {
            cast(str, key): _hash_safe_json(item, f"{field_name}.{key}")
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [
            _hash_safe_json(item, f"{field_name}[{index}]") for index, item in enumerate(sequence)
        ]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"{field_name} contains unsupported hash value {type(value).__name__}")


def internal_record_sha256(value: object) -> str:
    """Hash a tagged internal record using ``YAYA_INTERNAL_RECORD_HASH_V1``.

    Internal records can contain bounded inference floats while the public
    canonical JSON v1 format deliberately accepts integer numbers only.  This
    adapter-level hash first uses the closed codec, then represents each finite
    float as its normalized decimal string under an explicit tag before calling
    the cross-language canonical encoder.
    """

    encoded = encode(value)
    normalized = _hash_safe_json(encoded)
    if not isinstance(normalized, Mapping):
        raise TypeError("internal record hash root must be a JSON object")
    return canonical_json_sha256(cast(Mapping[str, object], normalized))


def agent_turn_commit_sha256(value: object) -> str:
    """Stable committed-AgentTurn digest stored by learner inference events."""

    return internal_record_sha256(value)


__all__ = [
    "agent_turn_commit_sha256",
    "decode",
    "decode_as",
    "encode",
    "internal_record_sha256",
    "plain",
]
