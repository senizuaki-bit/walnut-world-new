from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.composition import verify_contract_manifest  # noqa: E402
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    ContentRef,
    RequestContext,
    StudentActivationScope,
    StudentActiveSkill,
    StudentBootstrapActivation,
    StudentBootstrapBuild,
    StudentBootstrapCapabilities,
    StudentBootstrapSession,
    StudentBootstrapV2,
    StudentBootstrapWorld,
    StudentSessionCreateRequest,
)


class StudentBootstrapV2Tests(unittest.TestCase):
    def bootstrap(self) -> StudentBootstrapV2:
        now = datetime(2026, 8, 11, 10, tzinfo=UTC)
        actor = ActorRef("tenant_yaya", "student_0001", ActorType.STUDENT, ("game:player",))
        content = ContentRef("YAYA_FARM_001", "1.4.0", "a" * 64)
        context = RequestContext(
            request_id="req_student_bootstrap_0001",
            correlation_id="corr_student_bootstrap_0001",
            trace_id="trace_student_bootstrap_0001",
            requested_at=now,
            actor=actor,
            content_ref=content,
        )
        create_request = StudentSessionCreateRequest(
            world_id="world_demo_001",
            learner_id="student_0001",
            agent_profile_id="profile_sprout_001",
            channel="GAME",
            locale="zh-CN",
            content=content,
            expected_world_revision=184,
        )
        return StudentBootstrapV2(
            request_context=context,
            server_time=now,
            actor=actor,
            content=content,
            capabilities=StudentBootstrapCapabilities(True, True, True, True, True),
            session=StudentBootstrapSession(
                current_session_id="session_student_0001",
                teaching_spec_version="agent-teaching-v1",
                create_request=create_request,
            ),
            build=StudentBootstrapBuild(
                build_policy_id="student-cpp-v1",
                compiler_profile="cpp20-restricted-v1",
                compiler_version="gcc-14.2.0",
                sandbox_image_digest=f"sha256:{'b' * 64}",
                test_suite_version="student-skill-v1",
                allowed_capabilities=("WORLD_READ", "MOVE", "WATER"),
            ),
            activation=StudentBootstrapActivation(
                scope=StudentActivationScope("world_demo_001", "profile_sprout_001"),
                registry_revision=7,
                active=StudentActiveSkill(
                    activation_id="activation_demo_0001",
                    skill_id="skill_watering_001",
                    skill_version_id="skillver_demo_0001",
                    artifact_sha256="c" * 64,
                    certification_id="cert_demo_00000001",
                    registry_revision=7,
                    activated_at=now,
                ),
            ),
            world=StudentBootstrapWorld(
                world_id="world_demo_001",
                revision=184,
                last_event_sequence=731,
                state_hash="d" * 64,
                snapshot_url="/v1/worlds/world_demo_001/snapshot",
                events_url="/v1/worlds/world_demo_001/events",
            ),
        )

    def test_schema_example_and_python_dto_are_valid(self) -> None:
        example = json.loads(
            (CONTRACTS_ROOT / "examples/game-student-bootstrap-v2.json").read_text(encoding="utf-8")
        )["value"]
        ContractSchemaValidator(CONTRACTS_ROOT).validate(
            "schemas/game/student-bootstrap-v2.schema.json", example
        )
        ContractSchemaValidator(CONTRACTS_ROOT).validate(
            "schemas/game/agent-session-create-request.schema.json",
            example["session"]["create_request"],
        )
        self.assertEqual(
            set(example["session"]["create_request"]),
            {
                "world_id",
                "learner_id",
                "agent_profile_id",
                "channel",
                "locale",
                "content",
                "expected_world_revision",
            },
        )
        self.assertEqual(
            example["session"]["create_request"]["expected_world_revision"],
            example["world"]["revision"],
        )
        bootstrap = self.bootstrap()
        self.assertEqual(bootstrap.api_version, "1.1.0")
        self.assertEqual(bootstrap.contract_version, "0.4.0")
        self.assertEqual(bootstrap.session.teaching_spec_version, "agent-teaching-v1")

    def test_cross_resource_authority_drift_is_rejected(self) -> None:
        bootstrap = self.bootstrap()
        wrong_world = replace(
            bootstrap.world,
            world_id="world_other_001",
            snapshot_url="/v1/worlds/world_other_001/snapshot",
            events_url="/v1/worlds/world_other_001/events",
        )
        with self.assertRaisesRegex(ValueError, "create_request world_id"):
            replace(bootstrap, world=wrong_world)
        with self.assertRaisesRegex(ValueError, "registry_revision"):
            replace(
                bootstrap.activation,
                registry_revision=bootstrap.activation.registry_revision + 1,
            )
        with self.assertRaisesRegex(ValueError, "expected_world_revision"):
            replace(
                bootstrap,
                session=replace(
                    bootstrap.session,
                    create_request=replace(
                        bootstrap.session.create_request,
                        expected_world_revision=bootstrap.world.revision + 1,
                    ),
                ),
            )

    def test_regenerated_manifest_cannot_accept_v03_byte_drift(self) -> None:
        verify_contract_manifest(CONTRACTS_ROOT)
        with tempfile.TemporaryDirectory(prefix="yaya-contract-v03-lock-") as raw_root:
            repository_copy = Path(raw_root) / "repository"
            copied_contracts = repository_copy / "contracts"
            shutil.copytree(CONTRACTS_ROOT, copied_contracts)
            manifest_path = copied_contracts / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            base_entry = next(
                entry
                for entry in manifest["files"]
                if entry["path"]
                not in {
                    "contracts/releases/agent-contracts-v0.3.lock.json",
                    "contracts/examples/game-student-bootstrap-v2.json",
                    "contracts/openapi/student-bootstrap-v2.openapi.json",
                    "contracts/schemas/game/student-bootstrap-v2.schema.json",
                }
            )
            target = repository_copy / base_entry["path"]
            payload = target.read_bytes() + b"\n"
            target.write_bytes(payload)
            base_entry["bytes"] = len(payload)
            base_entry["sha256"] = hashlib.sha256(payload).hexdigest()
            manifest_path.write_text(
                f"{json.dumps(manifest, indent=2)}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "v0.3 frozen manifest entries drifted"):
                verify_contract_manifest(copied_contracts)


if __name__ == "__main__":
    unittest.main()
