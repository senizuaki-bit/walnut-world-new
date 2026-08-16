"""Fail-closed JSON Schema subset used for Agent tool arguments.

Tool schemas are intentionally small and local.  The validator rejects every
unsupported schema keyword at registration, so a typo can never weaken input
validation.  Public Wire schemas continue to use the full Draft 2020-12
validator in the existing contract layer.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import NoReturn, cast

from .errors import AgentConfigurationError, AgentToolInputError

_SCHEMA_KEYS = frozenset(
    {
        "type",
        "description",
        "title",
        "additionalProperties",
        "required",
        "properties",
        "items",
        "prefixItems",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "pattern",
        "oneOf",
    }
)
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def _schema_error(message: str, *, path: str, keyword: str | None = None) -> NoReturn:
    details: dict[str, object] = {"path": path}
    if keyword is not None:
        details["keyword"] = keyword
    raise AgentConfigurationError("TOOL_SCHEMA_INVALID", message, details)


def _input_error(message: str, *, path: str, keyword: str) -> NoReturn:
    raise AgentToolInputError(
        "TOOL_INPUT_INVALID",
        message,
        {"path": path, "keyword": keyword},
    )


def validate_schema_definition(schema: Mapping[str, object], *, path: str = "$") -> None:
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        _schema_error(
            "tool schema contains unsupported keywords",
            path=path,
            keyword=sorted(unknown)[0],
        )
    schema_type = schema.get("type")
    one_of = schema.get("oneOf")
    if (schema_type is None) == (one_of is None):
        _schema_error("tool schema must declare exactly one of type or oneOf", path=path)
    if one_of is not None:
        if isinstance(one_of, (str, bytes, bytearray)) or not isinstance(one_of, Sequence):
            _schema_error("oneOf must be an array", path=path, keyword="oneOf")
        variants = tuple(cast(Sequence[object], one_of))
        if len(variants) < 2:
            _schema_error("oneOf must contain at least two variants", path=path, keyword="oneOf")
        for index, variant in enumerate(variants):
            if not isinstance(variant, Mapping):
                _schema_error("oneOf variants must be objects", path=f"{path}.oneOf[{index}]")
            validate_schema_definition(
                cast(Mapping[str, object], variant),
                path=f"{path}.oneOf[{index}]",
            )
        return
    if not isinstance(schema_type, str) or schema_type not in _TYPES:
        _schema_error("type is not supported", path=path, keyword="type")

    if "enum" in schema:
        enum = schema["enum"]
        if isinstance(enum, (str, bytes, bytearray)) or not isinstance(enum, Sequence) or not enum:
            _schema_error("enum must be a non-empty array", path=path, keyword="enum")
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            _schema_error(
                "every tool input object must set additionalProperties to false",
                path=path,
                keyword="additionalProperties",
            )
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping):
            _schema_error("object properties must be an object", path=path, keyword="properties")
        if isinstance(required, (str, bytes, bytearray)) or not isinstance(required, Sequence):
            _schema_error("object required must be an array", path=path, keyword="required")
        properties_value = cast(Mapping[object, object], properties)
        property_names = set(properties_value)
        required_names = tuple(cast(Sequence[object], required))
        if any(not isinstance(item, str) for item in required_names):
            _schema_error("required items must be strings", path=path, keyword="required")
        required_strings = cast(tuple[str, ...], required_names)
        if (
            len(required_strings) != len(set(required_strings))
            or not set(required_strings) <= property_names
        ):
            _schema_error(
                "required must contain unique declared property names",
                path=path,
                keyword="required",
            )
        for name, child in properties_value.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                _schema_error("properties entries must be named schemas", path=path)
            validate_schema_definition(
                cast(Mapping[str, object], child),
                path=f"{path}.properties.{name}",
            )
    elif schema_type == "array":
        items = schema.get("items")
        if items is not False and not isinstance(items, Mapping):
            _schema_error(
                "array items must be a schema or false",
                path=path,
                keyword="items",
            )
        prefix_items = schema.get("prefixItems")
        if prefix_items is not None:
            if (
                isinstance(prefix_items, (str, bytes, bytearray))
                or not isinstance(prefix_items, Sequence)
                or not prefix_items
            ):
                _schema_error(
                    "prefixItems must be a non-empty array of schemas",
                    path=path,
                    keyword="prefixItems",
                )
            for index, child in enumerate(cast(Sequence[object], prefix_items)):
                if not isinstance(child, Mapping):
                    _schema_error(
                        "prefixItems entries must be schemas",
                        path=f"{path}.prefixItems[{index}]",
                        keyword="prefixItems",
                    )
                validate_schema_definition(
                    cast(Mapping[str, object], child),
                    path=f"{path}.prefixItems[{index}]",
                )
        if isinstance(items, Mapping):
            validate_schema_definition(
                cast(Mapping[str, object], items),
                path=f"{path}.items",
            )
    for minimum_key, maximum_key in (
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
    ):
        for key in (minimum_key, maximum_key):
            if key in schema:
                boundary = schema[key]
                if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
                    _schema_error(f"{key} must be a non-negative integer", path=path, keyword=key)
        if minimum_key in schema and maximum_key in schema:
            if cast(int, schema[minimum_key]) > cast(int, schema[maximum_key]):
                _schema_error("schema minimum exceeds maximum", path=path)
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            _schema_error("pattern must be text", path=path, keyword="pattern")
        try:
            re.compile(pattern)
        except re.error as error:
            _schema_error(f"pattern is invalid: {error}", path=path, keyword="pattern")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        _schema_error("uniqueItems must be boolean", path=path, keyword="uniqueItems")
    for key in ("minimum", "maximum"):
        if key in schema:
            _require_finite_schema_number(schema[key], path=path, keyword=key)
    if "minimum" in schema and "maximum" in schema:
        if Decimal(str(schema["minimum"])) > Decimal(str(schema["maximum"])):
            _schema_error("schema minimum exceeds maximum", path=path)
    if "multipleOf" in schema:
        multiple = _require_finite_schema_number(
            schema["multipleOf"],
            path=path,
            keyword="multipleOf",
        )
        if multiple <= 0:
            _schema_error(
                "multipleOf must be greater than zero",
                path=path,
                keyword="multipleOf",
            )


def validate_instance(
    instance: object,
    schema: Mapping[str, object],
    *,
    path: str = "$",
) -> None:
    """Validate one tool argument value against a pre-validated schema."""

    if "oneOf" in schema:
        variants = cast(Sequence[Mapping[str, object]], schema["oneOf"])
        matches = 0
        for variant in variants:
            try:
                validate_instance(instance, variant, path=path)
            except AgentToolInputError:
                continue
            matches += 1
        if matches != 1:
            _input_error(
                "value must match exactly one declared schema variant",
                path=path,
                keyword="oneOf",
            )
        return

    if "const" in schema and instance != schema["const"]:
        _input_error("value does not equal the required constant", path=path, keyword="const")
    if "enum" in schema and instance not in cast(Sequence[object], schema["enum"]):
        _input_error("value is not in the allowed enum", path=path, keyword="enum")
    schema_type = cast(str, schema["type"])
    if schema_type == "null":
        if instance is not None:
            _input_error("value must be null", path=path, keyword="type")
        return
    if schema_type == "boolean":
        if not isinstance(instance, bool):
            _input_error("value must be boolean", path=path, keyword="type")
        return
    if schema_type == "integer":
        if isinstance(instance, bool) or not isinstance(instance, int):
            _input_error("value must be an integer", path=path, keyword="type")
        _validate_numeric_bounds(instance, schema, path)
        return
    if schema_type == "number":
        if isinstance(instance, bool) or not isinstance(instance, (int, float)):
            _input_error("value must be a number", path=path, keyword="type")
        if isinstance(instance, float) and (
            instance != instance or instance in (float("inf"), float("-inf"))
        ):
            _input_error("number must be finite", path=path, keyword="type")
        _validate_numeric_bounds(instance, schema, path)
        return
    if schema_type == "string":
        if not isinstance(instance, str):
            _input_error("value must be text", path=path, keyword="type")
        if "minLength" in schema and len(instance) < cast(int, schema["minLength"]):
            _input_error("text is shorter than minLength", path=path, keyword="minLength")
        if "maxLength" in schema and len(instance) > cast(int, schema["maxLength"]):
            _input_error("text is longer than maxLength", path=path, keyword="maxLength")
        if "pattern" in schema and re.fullmatch(cast(str, schema["pattern"]), instance) is None:
            _input_error("text does not match pattern", path=path, keyword="pattern")
        return
    if schema_type == "array":
        if isinstance(instance, (str, bytes, bytearray)) or not isinstance(instance, Sequence):
            _input_error("value must be an array", path=path, keyword="type")
        items = tuple(cast(Sequence[object], instance))
        if "minItems" in schema and len(items) < cast(int, schema["minItems"]):
            _input_error("array is shorter than minItems", path=path, keyword="minItems")
        if "maxItems" in schema and len(items) > cast(int, schema["maxItems"]):
            _input_error("array is longer than maxItems", path=path, keyword="maxItems")
        if schema.get("uniqueItems") is True:
            canonical = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items]
            if len(canonical) != len(set(canonical)):
                _input_error("array items must be unique", path=path, keyword="uniqueItems")
        raw_prefix = schema.get("prefixItems", ())
        prefix_schemas = cast(Sequence[Mapping[str, object]], raw_prefix)
        for index, child_schema in enumerate(prefix_schemas[: len(items)]):
            validate_instance(items[index], child_schema, path=f"{path}[{index}]")
        remaining_start = min(len(prefix_schemas), len(items))
        additional_schema = schema["items"]
        if additional_schema is False:
            if len(items) > len(prefix_schemas):
                _input_error(
                    "array contains items beyond its declared prefix",
                    path=f"{path}[{len(prefix_schemas)}]",
                    keyword="items",
                )
        else:
            child_schema = cast(Mapping[str, object], additional_schema)
            for index in range(remaining_start, len(items)):
                validate_instance(items[index], child_schema, path=f"{path}[{index}]")
        return
    if schema_type == "object":
        if not isinstance(instance, Mapping):
            _input_error("value must be an object", path=path, keyword="type")
        value = cast(Mapping[str, object], instance)
        properties = cast(Mapping[str, Mapping[str, object]], schema["properties"])
        required = cast(Sequence[str], schema["required"])
        missing = set(required) - set(value)
        extra = set(value) - set(properties)
        if missing:
            _input_error(
                f"object is missing required property {sorted(missing)[0]}",
                path=path,
                keyword="required",
            )
        if extra:
            _input_error(
                f"object contains undeclared property {sorted(extra)[0]}",
                path=path,
                keyword="additionalProperties",
            )
        for name, item in value.items():
            validate_instance(item, properties[name], path=f"{path}.{name}")
        return
    raise AssertionError(f"unreachable validated schema type: {schema_type}")


def _validate_numeric_bounds(
    instance: int | float,
    schema: Mapping[str, object],
    path: str,
) -> None:
    if "minimum" in schema:
        minimum = schema["minimum"]
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            _schema_error("minimum must be numeric", path=path, keyword="minimum")
        if instance < minimum:
            _input_error("number is below minimum", path=path, keyword="minimum")
    if "maximum" in schema:
        maximum = schema["maximum"]
        if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
            _schema_error("maximum must be numeric", path=path, keyword="maximum")
        if instance > maximum:
            _input_error("number is above maximum", path=path, keyword="maximum")
    if "multipleOf" in schema:
        multiple = cast(int | float, schema["multipleOf"])
        if Decimal(str(instance)) % Decimal(str(multiple)) != 0:
            _input_error(
                "number is not an exact multipleOf",
                path=path,
                keyword="multipleOf",
            )


def _require_finite_schema_number(
    value: object,
    *,
    path: str,
    keyword: str,
) -> Decimal:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        _schema_error(
            f"{keyword} must be a finite number",
            path=path,
            keyword=keyword,
        )
    return Decimal(str(value))


__all__ = ["validate_instance", "validate_schema_definition"]
