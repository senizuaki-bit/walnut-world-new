"""Frozen-contract JSON validation and transport conversion helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema

from .codec import plain


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate schema JSON key {key}")
        result[key] = value
    return result


class ContractSchemaValidator:
    """Load the deployed frozen schemas once and validate exact wire values."""

    def __init__(self, contracts_root: Path) -> None:
        root = contracts_root.expanduser().resolve()
        schema_root = root / "schemas"
        manifest = root / "manifest.json"
        if not schema_root.is_dir() or not manifest.is_file():
            raise RuntimeError("deployed frozen contracts directory is incomplete")
        registry: Registry[Schema] = Registry()
        schemas: dict[Path, Schema] = {}
        for path in sorted(schema_root.rglob("*.schema.json")):
            decoded = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_object,
            )
            if not isinstance(decoded, Mapping):
                raise RuntimeError(f"contract schema is not an object: {path}")
            schema = cast(Schema, decoded)
            resolved = path.resolve()
            schemas[resolved] = schema
            resource = Resource(contents=schema, specification=DRAFT202012)
            registry = registry.with_resource(resolved.as_uri(), resource)
            schema_id = cast(Mapping[str, object], decoded).get("$id")
            if isinstance(schema_id, str):
                registry = registry.with_resource(schema_id, resource)
        if not schemas:
            raise RuntimeError("no frozen JSON schemas were deployed")
        self._contracts_root = root
        self._schemas = schemas
        self._registry = registry

    @property
    def contracts_root(self) -> Path:
        return self._contracts_root

    def validate(self, relative_path: str, value: Mapping[str, object]) -> None:
        path = (self._contracts_root / relative_path).resolve()
        try:
            path.relative_to(self._contracts_root)
        except ValueError as error:
            raise ValueError("contract schema path escaped contracts root") from error
        schema = self._schemas.get(path)
        if schema is None:
            raise RuntimeError(f"required frozen schema is unavailable: {relative_path}")
        schema_object = cast(dict[str, Any], schema)
        validator_type = validator_for(schema_object)
        validator_type.check_schema(schema_object)
        validator = validator_type(
            schema_object,
            registry=self._registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(
            validator.iter_errors(cast(Any, value)),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            first = errors[0]
            location = "/".join(str(part) for part in first.absolute_path) or "$"
            raise ValueError(f"value violates {relative_path} at {location}: {first.message}")

    def validate_reference(self, reference: str, value: Mapping[str, object]) -> None:
        """Validate a value against one frozen schema URI (including a fragment)."""

        if not reference.startswith("https://contracts.yaya.local/"):
            raise ValueError("contract reference is outside the frozen registry")
        schema: dict[str, object] = {"$ref": reference}
        validator = validator_for(schema)(
            schema,
            registry=self._registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(
            validator.iter_errors(cast(Any, value)),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            first = errors[0]
            location = "/".join(str(part) for part in first.absolute_path) or "$"
            raise ValueError(f"value violates {reference} at {location}: {first.message}")


def wire_object(value: object) -> dict[str, object]:
    converted = plain(value)
    if not isinstance(converted, Mapping):
        raise TypeError("wire value must convert to a JSON object")
    mapping = cast(Mapping[object, object], converted)
    if any(not isinstance(key, str) for key in mapping):
        raise TypeError("wire object keys must be strings")
    return {cast(str, key): item for key, item in mapping.items()}


__all__ = ["ContractSchemaValidator", "wire_object"]
