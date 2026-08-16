"""Skill Build command acceptance and source-bundle invariants."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from yaya_agent_contracts import (
    CommandCreateReceipt,
    NewCommand,
    OperationContext,
    Result,
    VersionSet,
)

from walnut_backend.adapters.postgres.skill_builds import PostgresSkillBuildStore


class InvalidSkillBuildRequest(ValueError):
    pass


class SkillBuildCommands:
    def __init__(self, store: PostgresSkillBuildStore) -> None:
        self._store = store

    async def accept(
        self, raw_body: bytes, idempotency_key: str, context: OperationContext
    ) -> Result[tuple[dict[str, Any], CommandCreateReceipt]]:
        body = parse_strict_object(raw_body)
        validate_source_bundle(body)
        command = NewCommand(
            command_type="CREATE_SKILL_BUILD",
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            versions=VersionSet(
                api_version="1.0.0",
                event_version="1",
                policy_version="policy-1",
                world_rules_version="rules-1",
                teaching_spec_version="teaching-1",
                test_suite_version=cast(str, body["test_suite_version"]),
            ),
        )
        return await self._store.accept(command, body, context)

    async def get(self, build_id: str, context: OperationContext) -> Result[dict[str, Any]]:
        return await self._store.get(build_id, context)


def parse_strict_object(raw_body: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise InvalidSkillBuildRequest(f"duplicate JSON key {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise InvalidSkillBuildRequest(f"invalid JSON number {value}")

    try:
        decoded = json.loads(
            raw_body.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidSkillBuildRequest("request body must be strict UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise InvalidSkillBuildRequest("request body must be a JSON object")
    return decoded


def validate_source_bundle(body: Mapping[str, Any]) -> None:
    source = body.get("source_bundle")
    if not isinstance(source, Mapping):
        raise InvalidSkillBuildRequest("source_bundle must be an object")
    files = source.get("files")
    if not isinstance(files, list):
        raise InvalidSkillBuildRequest("source_bundle.files must be an array")
    paths: set[str] = set()
    source_bytes = 0
    for item in files:
        if not isinstance(item, Mapping):
            raise InvalidSkillBuildRequest("source_bundle.files entries must be objects")
        path, content, content_sha256 = item.get("path"), item.get("content"), item.get("content_sha256")
        if not isinstance(path, str) or not isinstance(content, str) or not isinstance(content_sha256, str):
            raise InvalidSkillBuildRequest("source bundle file fields are invalid")
        if path in paths:
            raise InvalidSkillBuildRequest("source bundle paths must be unique")
        paths.add(path)
        encoded = content.encode("utf-8")
        source_bytes += len(encoded)
        if hashlib.sha256(encoded).hexdigest() != content_sha256:
            raise InvalidSkillBuildRequest("source bundle content hash does not match bytes")
    if len(files) > 32 or source_bytes > 1_048_576:
        raise InvalidSkillBuildRequest("source bundle exceeds released limits")
    if source.get("entrypoint") not in paths:
        raise InvalidSkillBuildRequest("source bundle entrypoint must name exactly one file")
