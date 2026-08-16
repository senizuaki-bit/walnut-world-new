"""Closed, certification-bound parameter schemas for executable Skills."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from jsonschema.validators import validator_for
from yaya_agent_contracts import canonical_json_sha256

POLICY_PARAMETER_SCHEMA_KEY = "parameter_schema"
CERTIFICATION_EXTENSION_KEY = "x-yaya-certification"
CERTIFIED_PARAMETER_SCHEMA_KEY = "parameter_schema"
CERTIFIED_PARAMETER_SCHEMA_SHA256_KEY = "parameter_schema_sha256"


class CertifiedSkillSchemaError(ValueError):
    """Durable policy/schema/certification bytes do not form one exact closure."""


def policy_parameter_schema(
    policy_json: Mapping[str, Any], *, policy_sha256: str
) -> dict[str, Any]:
    """Return a validated server-owned base schema from exact policy bytes."""

    if canonical_json_sha256(policy_json) != policy_sha256:
        raise CertifiedSkillSchemaError("Build policy hash drifted")
    raw = policy_json.get(POLICY_PARAMETER_SCHEMA_KEY)
    if not isinstance(raw, Mapping):
        raise CertifiedSkillSchemaError("Build policy parameter schema is not an object")
    schema = deepcopy(dict(raw))
    if CERTIFICATION_EXTENSION_KEY in schema:
        raise CertifiedSkillSchemaError(
            "Build policy parameter schema contains reserved certification metadata"
        )
    if ("type" in schema) == ("oneOf" in schema):
        raise CertifiedSkillSchemaError(
            "Build policy parameter schema must declare exactly one of type or oneOf"
        )
    try:
        validator_for(schema).check_schema(schema)
    except Exception as error:
        raise CertifiedSkillSchemaError("Build policy parameter schema is invalid") from error
    return schema


def certified_parameter_schema(
    policy_json: Mapping[str, Any],
    *,
    policy_sha256: str,
    build_id: str,
    skill_id: str,
    skill_version_id: str,
    source_sha256: str,
    artifact_sha256: str,
    certification_id: str,
    build_policy_id: str,
    actor_id: str,
    content_hash: str,
    capabilities: Sequence[str],
) -> tuple[dict[str, Any], str]:
    """Bind a validated base schema to one durable certification tuple."""

    schema = policy_parameter_schema(policy_json, policy_sha256=policy_sha256)
    schema[CERTIFICATION_EXTENSION_KEY] = _certification_metadata(
        build_id=build_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        source_sha256=source_sha256,
        artifact_sha256=artifact_sha256,
        certification_id=certification_id,
        build_policy_id=build_policy_id,
        policy_sha256=policy_sha256,
        actor_id=actor_id,
        content_hash=content_hash,
        capabilities=capabilities,
    )
    return schema, canonical_json_sha256(schema)


def validated_certified_parameter_schema(
    policy_json: Mapping[str, Any],
    artifact_metadata: Mapping[str, Any],
    certification_json: Mapping[str, Any],
    *,
    policy_sha256: str,
    build_id: str,
    skill_id: str,
    skill_version_id: str,
    source_sha256: str,
    artifact_sha256: str,
    certification_id: str,
    build_policy_id: str,
    actor_id: str,
    content_hash: str,
    capabilities: Sequence[str],
) -> dict[str, Any]:
    """Verify both durable copies and return the exact certified schema."""

    expected, expected_sha256 = certified_parameter_schema(
        policy_json,
        policy_sha256=policy_sha256,
        build_id=build_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        source_sha256=source_sha256,
        artifact_sha256=artifact_sha256,
        certification_id=certification_id,
        build_policy_id=build_policy_id,
        actor_id=actor_id,
        content_hash=content_hash,
        capabilities=capabilities,
    )
    for label, container in (
        ("Artifact", artifact_metadata),
        ("Certification", certification_json),
    ):
        schema = container.get(CERTIFIED_PARAMETER_SCHEMA_KEY)
        digest = container.get(CERTIFIED_PARAMETER_SCHEMA_SHA256_KEY)
        if (
            not isinstance(schema, Mapping)
            or dict(schema) != expected
            or digest != expected_sha256
            or canonical_json_sha256(schema) != expected_sha256
        ):
            raise CertifiedSkillSchemaError(f"{label} certified parameter schema closure drifted")
    return expected


def _certification_metadata(
    *,
    build_id: str,
    skill_id: str,
    skill_version_id: str,
    source_sha256: str,
    artifact_sha256: str,
    certification_id: str,
    build_policy_id: str,
    policy_sha256: str,
    actor_id: str,
    content_hash: str,
    capabilities: Sequence[str],
) -> dict[str, Any]:
    if (
        isinstance(capabilities, str | bytes | bytearray)
        or any(not isinstance(item, str) or not item for item in capabilities)
        or len(set(capabilities)) != len(capabilities)
    ):
        raise CertifiedSkillSchemaError("Certification capabilities are invalid")
    values = {
        "build_id": build_id,
        "skill_id": skill_id,
        "skill_version_id": skill_version_id,
        "source_sha256": source_sha256,
        "artifact_sha256": artifact_sha256,
        "certification_id": certification_id,
        "build_policy_id": build_policy_id,
        "policy_sha256": policy_sha256,
        "actor_id": actor_id,
        "content_hash": content_hash,
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise CertifiedSkillSchemaError("Certification tuple is invalid")
    return {
        "schema_version": "1.0.0",
        **values,
        "capabilities": list(capabilities),
    }
