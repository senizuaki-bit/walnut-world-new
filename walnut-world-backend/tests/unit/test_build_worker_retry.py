"""Retry classification at the Build worker/library boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from yaya_agent_build import DockerBuildFailure, DockerBuildResult

from walnut_backend.workers.build_worker import (
    BuildInfrastructureRetry,
    BuildWorkflowHandler,
)


def test_retryable_docker_failure_is_not_published_as_build_rejection() -> None:
    rejected = False
    claim = cast(
        Any,
        SimpleNamespace(operation="CREATE_SKILL_BUILD", build_id="build_retry_boundary_0001"),
    )
    authority = cast(Any, SimpleNamespace(claim=claim))
    result = DockerBuildResult(
        build_id="build_retry_boundary_0001",
        status="FAILED",
        source_sha256="1" * 64,
        compiler_profile="YAYA_CPP20_SAFE_V1",
        compiler_version="gcc-14.2.0",
        test_suite_version="suite-retry-1",
        build_identity="2" * 64,
        workspace=None,
        staged_artifact=None,
        artifact_sha256=None,
        tests=(),
        diagnostics=(),
        failure=DockerBuildFailure(
            code="DOCKER_OUTCOME_UNKNOWN",
            stage="COMPILE",
            diagnostics=(),
            retryable=True,
        ),
    )
    handler = cast(Any, object.__new__(BuildWorkflowHandler))

    async def prepare(_claim: object) -> object:
        return authority

    async def build(_authority: object) -> tuple[DockerBuildResult, object]:
        return result, claim

    async def finish_rejected(*_args: object) -> None:
        nonlocal rejected
        rejected = True

    handler._prepare = prepare
    handler._build_with_heartbeat = build
    handler._finish_rejected = finish_rejected

    with pytest.raises(BuildInfrastructureRetry, match="COMPILE:DOCKER_OUTCOME_UNKNOWN"):
        asyncio.run(handler.execute(claim))
    assert rejected is False


def test_nonretryable_compile_failure_still_closes_as_rejection() -> None:
    rejected = False
    claim = cast(Any, SimpleNamespace(operation="CREATE_SKILL_BUILD"))
    authority = cast(Any, SimpleNamespace(claim=claim))
    result = DockerBuildResult(
        build_id="build_rejected_boundary_0001",
        status="FAILED",
        source_sha256="3" * 64,
        compiler_profile="YAYA_CPP20_SAFE_V1",
        compiler_version="gcc-14.2.0",
        test_suite_version="suite-rejected-1",
        build_identity="4" * 64,
        workspace=None,
        staged_artifact=None,
        artifact_sha256=None,
        tests=(),
        diagnostics=(),
        failure=DockerBuildFailure(
            code="COMPILE_ERROR",
            stage="COMPILE",
            diagnostics=(),
            retryable=False,
        ),
    )
    handler = cast(Any, object.__new__(BuildWorkflowHandler))

    async def prepare(_claim: object) -> object:
        return authority

    async def build(_authority: object) -> tuple[DockerBuildResult, object]:
        return result, claim

    async def finish_rejected(*_args: object) -> None:
        nonlocal rejected
        rejected = True

    handler._prepare = prepare
    handler._build_with_heartbeat = build
    handler._finish_rejected = finish_rejected

    asyncio.run(handler.execute(claim))
    assert rejected is True
