"""Compose keeps nested Docker and its runtime volume in one daemon namespace."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_DIND_DIGEST = "a" * 64
_SANDBOX_DIGEST = "b" * 64
_BUILD_DIGEST = "c" * 64
_POSTGRES_DIGEST = "d" * 64


def test_compose_worker_uses_private_dind_and_shared_named_runtime_volume() -> None:
    environment = _compose_environment()
    completed = subprocess.run(
        [sys.executable, "scripts/run_compose.py", "config", "--format", "json"],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    config = cast(dict[str, Any], json.loads(completed.stdout))
    services = cast(dict[str, Any], config["services"])
    worker = cast(dict[str, Any], services["workflow-worker"])
    learner = cast(dict[str, Any], services["learner-worker"])
    relay = cast(dict[str, Any], services["llm-relay"])
    engine = cast(dict[str, Any], services["docker-engine"])
    loader = cast(dict[str, Any], services["sandbox-image"])
    postgres = cast(dict[str, Any], services["postgres"])
    gateway = cast(dict[str, Any], services["backend"])

    build_image = f"walnut/backend@sha256:{_BUILD_DIGEST}"
    assert services["migrate"]["image"] == build_image
    assert services["backend"]["image"] == build_image
    assert worker["image"] == build_image
    assert learner["image"] == build_image
    assert relay["image"] == build_image
    assert "build" not in services["migrate"]
    assert "build" not in services["backend"]
    assert "build" not in worker
    assert "build" not in learner
    assert "build" not in relay
    assert worker["environment"]["WALNUT_RUNTIME_ROOT"] == "/var/lib/walnut"
    assert worker["environment"]["WALNUT_LLM_RELAY_ENDPOINT"] == "http://127.0.0.1:8081"
    assert worker["environment"]["WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST"] == "true"
    assert worker["network_mode"] == "service:llm-relay"
    assert relay["environment"]["WALNUT_LLM_RELAY_BIND_HOST"] == "127.0.0.1"
    assert relay["environment"]["WALNUT_LLM_RELAY_SERVER_API_KEY"] == (
        worker["environment"]["WALNUT_LLM_RELAY_API_KEY"]
    )
    assert "WALNUT_LLM_UPSTREAM_API_KEY" in relay["environment"]
    assert "WALNUT_LLM_UPSTREAM_API_KEY" not in worker["environment"]
    assert "WALNUT_LLM_UPSTREAM_API_KEY" not in learner["environment"]
    assert "WALNUT_LLM_RELAY_API_KEY" not in learner["environment"]
    assert "WALNUT_FEISHU_PSEUDONYM_SECRET" not in learner["environment"]
    assert "WALNUT_FEISHU_PSEUDONYM_SECRET" not in worker["environment"]
    assert "WALNUT_FEISHU_PSEUDONYM_SECRET" not in relay["environment"]
    assert gateway["environment"]["WALNUT_FEISHU_PSEUDONYM_SECRET"] == (
        environment["WALNUT_FEISHU_PSEUDONYM_SECRET"]
    )
    assert gateway["environment"]["WALNUT_FEISHU_MCP_DASHBOARD_URL"] == (
        environment["WALNUT_FEISHU_MCP_DASHBOARD_URL"]
    )
    assert gateway["environment"]["WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL"] == (
        environment["WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL"]
    )
    for service in (worker, learner, relay):
        assert "WALNUT_FEISHU_MCP_DASHBOARD_URL" not in service["environment"]
        assert "WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL" not in service["environment"]
    assert "WALNUT_SANDBOX_IMAGE" not in learner["environment"]
    assert "DOCKER_HOST" not in learner["environment"]
    assert "ports" not in learner
    assert learner["command"] == ["python", "-m", "walnut_backend.learner_worker_main"]
    assert "ports" not in relay
    assert gateway["ports"] == [
        {
            "mode": "ingress",
            "target": 8000,
            "published": "8790",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert all(
        service_name == "backend" or "ports" not in service
        for service_name, service in services.items()
    )
    assert "WALNUT_LLM_ENDPOINT" not in worker["environment"]
    assert "WALNUT_LLM_API_KEY" not in worker["environment"]
    assert worker["environment"]["DOCKER_HOST"] == ("unix:///var/run/walnut-docker/docker.sock")
    assert engine["privileged"] is True
    assert engine["image"].endswith(f"@sha256:{_DIND_DIGEST}")
    assert loader["image"] == engine["image"]
    assert postgres["image"] == f"postgres:16.9-alpine@sha256:{_POSTGRES_DIGEST}"
    assert _mount(worker, "/var/lib/walnut") == ("volume", "walnut-runtime")
    assert _mount(engine, "/var/lib/walnut") == ("volume", "walnut-runtime")
    assert _mount(worker, "/var/run/walnut-docker") == (
        "volume",
        "walnut-docker-socket",
    )
    assert _mount(engine, "/var/run/walnut-docker") == (
        "volume",
        "walnut-docker-socket",
    )
    assert all(
        mount.get("source") != "/var/run/docker.sock"
        for mount in cast(list[dict[str, Any]], worker["volumes"])
    )


@pytest.mark.parametrize("compose_arguments", (("config",), ("up", "--detach")))
@pytest.mark.parametrize(
    "name",
    (
        "WALNUT_BUILD_IMAGE",
        "WALNUT_DIND_IMAGE",
        "WALNUT_POSTGRES_IMAGE",
        "WALNUT_SANDBOX_IMAGE",
    ),
)
def test_compose_entrypoint_refuses_floating_images_before_config_or_start(
    name: str, compose_arguments: tuple[str, ...]
) -> None:
    environment = _compose_environment()
    environment[name] = "example/floating:latest"
    completed = subprocess.run(
        [sys.executable, "scripts/run_compose.py", *compose_arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == (
        f"COMPOSE_IMAGE_POLICY_REFUSED: {name} must be "
        "name@sha256:<64 lowercase hex characters>"
    )


def test_compose_config_fails_closed_without_feishu_pseudonym_secret() -> None:
    environment = _compose_environment()
    environment.pop("WALNUT_FEISHU_PSEUDONYM_SECRET", None)
    completed = subprocess.run(
        [sys.executable, "scripts/run_compose.py", "config"],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "WALNUT_FEISHU_PSEUDONYM_SECRET" in completed.stderr


def _compose_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "POSTGRES_PASSWORD": "compose-contract-password",
            "WALNUT_AUTH_HMAC_SECRET": "compose-contract-hmac-secret-0000000000000000",
            "WALNUT_AUTH_ISSUER": "compose-contract",
            "WALNUT_AUTH_AUDIENCE": "compose-contract",
            "WALNUT_FEISHU_PSEUDONYM_SECRET": (
                "compose-contract-feishu-pseudonym-secret-0000000000000000"
            ),
            "WALNUT_FEISHU_MCP_DASHBOARD_URL": (
                "https://example.feishu.cn/base/Base?table=Dashboard"
            ),
            "WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL": "https://teacher.example/app",
            "WALNUT_TENANT_ID": "tenant_compose_contract",
            "WALNUT_BUILD_IMAGE": f"walnut/backend@sha256:{_BUILD_DIGEST}",
            "WALNUT_DIND_IMAGE": f"docker:29-dind@sha256:{_DIND_DIGEST}",
            "WALNUT_POSTGRES_IMAGE": (
                f"postgres:16.9-alpine@sha256:{_POSTGRES_DIGEST}"
            ),
            "WALNUT_SANDBOX_IMAGE": f"gcc:14.2.0@sha256:{_SANDBOX_DIGEST}",
            "WALNUT_LLM_RELAY_API_KEY": "compose-contract-key",
            "WALNUT_LLM_UPSTREAM_API_KEY": "compose-upstream-key",
            "WALNUT_LLM_MODEL": "compose-contract-model",
            "WALNUT_LLM_PROVIDER": "compose-contract-provider",
            "WALNUT_PROMPT_VERSION": "prompt-v1",
            "WALNUT_TEACHING_SPEC_VERSION": "teaching-v1",
            "WALNUT_WORLD_RULES_VERSION": "world-v1",
            "WALNUT_WORLD_CONTENT_VERSION": "1.0.0",
        }
    )
    return environment


def _mount(service: dict[str, Any], target: str) -> tuple[str, str]:
    matches = [
        mount
        for mount in cast(list[dict[str, Any]], service["volumes"])
        if mount.get("target") == target
    ]
    assert len(matches) == 1
    return str(matches[0]["type"]), str(matches[0]["source"])
