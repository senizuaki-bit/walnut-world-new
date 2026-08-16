from __future__ import annotations

import inspect
import json
import re
import sys
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from jsonschema import FormatChecker

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

import yaya_agent_contracts.ports as ports_module  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    AuditPort,
    AuditQuery,
    AuditRecord,
    BuildArtifact,
    CertificationEvidence,
    CommandCreateReceipt,
    CommandRecord,
    CommandStatus,
    CommandStorePort,
    CommandTransition,
    CommandType,
    ContentRef,
    ContractError,
    DeliveryPayload,
    DeliveryPort,
    DeliveryReceipt,
    DomainEvent,
    ErrorCategory,
    EventAppendReceipt,
    EventStorePort,
    EvidenceRef,
    EvidenceType,
    Failure,
    FeishuPort,
    FeishuReportDraftBody,
    LearnerPort,
    LlmMessage,
    LlmPort,
    LlmReply,
    LlmRequest,
    NewCommand,
    OperationContext,
    OutboxMessage,
    OutboxPort,
    OutboxStatus,
    PolicyPort,
    RegistryPort,
    RequestContext,
    RuntimeEvent,
    RuntimeEventType,
    SandboxPort,
    SandboxRunResult,
    SandboxUsage,
    SkillRef,
    SkillSourceBundle,
    SkillSourceFile,
    Success,
    TestCaseResult,
    UncommittedEvent,
    VersionSet,
    WaterIntent,
    WorldAtomicCommit,
    WorldAtomicCommitReceipt,
    WorldCommand,
    WorldCommitReceipt,
    WorldPort,
    WorldSnapshot,
    WorldUnitOfWorkPort,
    learner_inference_sha256,
)
from yaya_agent_contracts.models import (  # noqa: E402
    _is_rfc3986_reference,
    _require_iso_datetime,
)


class PythonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.content = ContentRef("YAYA_FARM_001", "1.4.0", "a" * 64)
        self.actor = ActorRef(
            "tenant_yaya",
            "student_0001",
            ActorType.STUDENT,
            ("game:player",),
        )
        self.context = RequestContext(
            request_id="req_python_0001",
            correlation_id="corr_python_0001",
            trace_id="trace_python_0001",
            requested_at=datetime.now(UTC),
            actor=self.actor,
            content_ref=self.content,
        )
        self.operation_context = OperationContext(
            request_id="req_python_0001",
            correlation_id="corr_python_0001",
            trace_id="trace_python_0001",
            requested_at=datetime.now(UTC),
            actor=self.actor,
            content_ref=self.content,
            command_id="cmd_python_0001",
            causation_id=None,
            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        self.versions = VersionSet(
            api_version="1.0.0",
            event_version="1",
            policy_version="policy-38",
            world_rules_version="farm-rules-12",
            teaching_spec_version="teaching-7",
        )
        self.error = ContractError(
            code="WORLD_REVISION_CONFLICT",
            category=ErrorCategory.CONCURRENCY,
            retryable=True,
            user_message_key="world.changed_retry",
            stage="WORLD_VALIDATE",
        )
        self.unknown_error = ContractError(
            code="UNKNOWN_COMMIT_STATE",
            category=ErrorCategory.DEPENDENCY,
            retryable=False,
            user_message_key="command.reconciling",
            stage="WORLD_COMMIT",
        )

    def command(
        self,
        status: CommandStatus,
        terminal: bool,
        *,
        result: dict[str, object] | None = None,
        error: ContractError | None = None,
        command_type: str = "EXECUTE_AGENT_TURN",
        stage: str | None = None,
    ) -> CommandRecord:
        now = datetime.now(UTC)
        if stage is None:
            if status is CommandStatus.UNKNOWN:
                stage = "WORLD_COMMIT"
            elif status is CommandStatus.ACCEPTED:
                stage = "ACCEPT"
            else:
                stage = "COMPLETE" if terminal else "VALIDATE"
        return CommandRecord(
            request_context=self.context,
            command_id="cmd_python_0001",
            command_type=command_type,
            status=status,
            stage=stage,
            terminal=terminal,
            accepted_at=now,
            updated_at=now,
            result=result,
            error=error,
            evidence_refs=(),
            versions=self.versions,
            links={"self": "/v1/commands/cmd_python_0001"},
        )

    def world_commit_result(self) -> dict[str, object]:
        return {
            "result_type": "WORLD_COMMIT",
            "world_id": "world_demo_001",
            "previous_revision": 184,
            "world_revision": 185,
            "first_event_sequence": 732,
            "last_event_sequence": 733,
        }

    def world_state(self) -> dict[str, object]:
        return {
            "clock": {"day": 1, "minute_of_day": 480, "tick": 100},
            "avatar": {
                "entity_id": "avatar_001",
                "position": {"x": 0, "y": 0},
                "energy": 100,
            },
            "inventory": [{"item_id": "seed.wheat", "quantity": 2}],
            "plots": [
                {
                    "plot_id": "plot_001",
                    "position": {"x": 1, "y": 2},
                    "soil_state": "TILLED",
                    "hydration": 500,
                    "crop": None,
                    "last_updated_event_sequence": 731,
                }
            ],
            "agents": [
                {
                    "entity_id": "agent_001",
                    "agent_profile_id": "profile_001",
                    "position": {"x": 3, "y": 4},
                    "activity": "IDLE",
                }
            ],
        }

    def event(
        self,
        event_id: str,
        sequence: int,
        *,
        stream_id: str = "world:demo",
        event_type: str = "world.plot_watered",
        payload: dict[str, object] | None = None,
        event_class: type[DomainEvent] = DomainEvent,
        event_version: int = 1,
        schema_version: str = "1.0.0",
        command_id: str = "cmd_python_0001",
        causation_id: str | None = "cmd_python_0001",
    ) -> DomainEvent:
        return event_class(
            event_id=event_id,
            event_type=event_type,
            event_version=event_version,
            stream_id=stream_id,
            sequence=sequence,
            occurred_at=datetime.now(UTC),
            producer="world-engine",
            trace_id="trace_python_0001",
            command_id=command_id,
            correlation_id="corr_python_0001",
            causation_id=causation_id,
            content_ref=self.content,
            payload=payload or {},
            schema_version=schema_version,
        )

    def test_domain_values_are_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.content.version = "2.0.0"  # type: ignore[misc]

    def test_json_values_are_recursively_frozen(self) -> None:
        source = {"inner": {"count": 1}, "items": [1, {"enabled": True}]}
        error = ContractError(
            code="INTERNAL_ERROR",
            category=ErrorCategory.INTERNAL,
            retryable=False,
            user_message_key="system.internal_error",
            stage="INTERNAL",
            details=source,
        )
        source["inner"]["count"] = 99  # type: ignore[index]
        source["items"].append(2)  # type: ignore[union-attr]
        self.assertEqual(error.details["inner"]["count"], 1)  # type: ignore[index]
        self.assertEqual(len(error.details["items"]), 2)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            error.details["inner"]["count"] = 2  # type: ignore[index, misc]

    def test_sequences_are_copied_and_normalized_to_tuples(self) -> None:
        roles = ["game:player"]
        actor = ActorRef("tenant_yaya", "student_0001", ActorType.STUDENT, roles)  # type: ignore[arg-type]
        roles.append("game:admin")
        self.assertIsInstance(actor.roles, tuple)
        self.assertEqual(actor.roles, ("game:player",))

    def test_schema_specific_identifiers_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "unit_id"):
            ContentRef("lowercase", "1.0.0", "a" * 64)
        for field_name, invalid_value in (
            ("request_id", "abc"),
            ("correlation_id", "correlation_python_0001"),
            ("trace_id", "tr_python_0001"),
        ):
            values = {
                "request_id": "req_python_0001",
                "correlation_id": "corr_python_0001",
                "trace_id": "trace_python_0001",
            }
            values[field_name] = invalid_value
            with (
                self.subTest(field_name=field_name),
                self.assertRaisesRegex(
                    ValueError,
                    field_name,
                ),
            ):
                RequestContext(
                    **values,
                    requested_at=datetime.now(UTC),
                    actor=self.actor,
                    content_ref=self.content,
                )
        with self.assertRaisesRegex(ValueError, "command_id"):
            OperationContext(
                request_id="req_python_0001",
                correlation_id="corr_python_0001",
                trace_id="trace_python_0001",
                requested_at=datetime.now(UTC),
                actor=self.actor,
                content_ref=self.content,
                command_id="command_python_0001",
                causation_id=None,
            )
        with self.assertRaisesRegex(ValueError, "event_id"):
            DomainEvent(
                event_id="event_wrong_0001",
                event_type="world.committed",
                event_version=1,
                stream_id="world:demo",
                sequence=1,
                occurred_at=datetime.now(UTC),
                producer="world-engine",
                trace_id="trace_python_0001",
                command_id="cmd_python_0001",
                correlation_id="corr_python_0001",
                causation_id="cmd_python_0001",
                content_ref=self.content,
                payload={},
            )

    def test_result_never_uses_none_or_boolean_for_failure(self) -> None:
        success = Success({"world_revision": 185})
        failure = Failure(self.error)
        self.assertTrue(success.ok)
        self.assertFalse(failure.ok)
        self.assertEqual(failure.error.code, "WORLD_REVISION_CONFLICT")
        with self.assertRaisesRegex(TypeError, "array-like sequence"):
            ActorRef(
                "tenant_yaya",
                "student_0001",
                ActorType.STUDENT,
                {"game:player": True},  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "ContractError"):
            Failure("not-an-error")  # type: ignore[arg-type]

    def test_python_values_cannot_serialize_non_rfc3339_offsets(self) -> None:
        seconds_offset = timezone(timedelta(seconds=30))
        with self.assertRaisesRegex(ValueError, "whole minutes"):
            EvidenceRef(
                "evidence_python_0001",
                EvidenceType.AUDIT_LOG,
                datetime(2026, 8, 7, tzinfo=seconds_offset),
            )

    def test_error_code_and_metadata_must_match_catalog(self) -> None:
        catalog_path = PACKAGE_ROOT.parent / "contracts" / "error-catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        for entry in catalog["errors"]:
            with self.subTest(catalog_code=entry["code"]):
                error = ContractError(
                    code=entry["code"],
                    category=ErrorCategory(entry["category"]),
                    retryable=entry["retryable"],
                    user_message_key=entry["user_message_key"],
                    stage="INTERNAL",
                )
                self.assertEqual(error.code, entry["code"])
        with self.assertRaisesRegex(ValueError, "not in the contract catalog"):
            ContractError(
                code="MADE_UP_ERROR",
                category=ErrorCategory.INTERNAL,
                retryable=False,
                user_message_key="system.internal_error",
                stage="INTERNAL",
            )
        for field_name, invalid_value in (
            ("category", ErrorCategory.INTERNAL),
            ("retryable", False),
            ("user_message_key", "world.wrong_key"),
        ):
            values = {
                "code": "WORLD_REVISION_CONFLICT",
                "category": ErrorCategory.CONCURRENCY,
                "retryable": True,
                "user_message_key": "world.changed_retry",
                "stage": "WORLD_VALIDATE",
            }
            values[field_name] = invalid_value
            with (
                self.subTest(field_name=field_name),
                self.assertRaisesRegex(
                    ValueError,
                    "metadata",
                ),
            ):
                ContractError(**values)  # type: ignore[arg-type]

    def test_optional_version_pins_cannot_be_empty(self) -> None:
        required = {
            "api_version": "1.0.0",
            "event_version": "1",
            "policy_version": "policy-38",
            "world_rules_version": "farm-rules-12",
            "teaching_spec_version": "teaching-7",
        }
        for name in (
            "skill_version",
            "compiler_version",
            "sandbox_image_digest",
            "test_suite_version",
            "prompt_version",
            "model_version",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                VersionSet(**required, **{name: ""})

    def test_unknown_is_terminal_and_requires_structured_error(self) -> None:
        command = self.command(CommandStatus.UNKNOWN, True, error=self.unknown_error)
        self.assertTrue(command.terminal)
        with self.assertRaisesRegex(ValueError, "terminal flag"):
            self.command(CommandStatus.UNKNOWN, False, error=self.unknown_error)
        with self.assertRaisesRegex(ValueError, "structured error"):
            self.command(CommandStatus.UNKNOWN, True)
        with self.assertRaisesRegex(ValueError, "UNKNOWN_COMMIT_STATE"):
            self.command(CommandStatus.UNKNOWN, True, error=self.error)
        with self.assertRaisesRegex(ValueError, "stage must be WORLD_COMMIT"):
            self.command(
                CommandStatus.UNKNOWN,
                True,
                error=self.unknown_error,
                stage="COMPLETE",
            )

    def test_applied_command_requires_committed_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "committed result"):
            self.command(CommandStatus.APPLIED, True)
        command = self.command(
            CommandStatus.APPLIED,
            True,
            result=self.world_commit_result(),
        )
        self.assertEqual(command.result["result_type"], "WORLD_COMMIT")  # type: ignore[index]

    def test_command_type_stage_and_result_union_are_strict(self) -> None:
        with self.assertRaisesRegex(ValueError, "command_type"):
            self.command(
                CommandStatus.APPLIED,
                True,
                result=self.world_commit_result(),
                command_type="DELETE_WORLD",
            )
        with self.assertRaisesRegex(ValueError, "stage must be COMPLETE"):
            self.command(
                CommandStatus.APPLIED,
                True,
                result=self.world_commit_result(),
                stage="WORLD_COMMIT",
            )
        incomplete = self.world_commit_result()
        del incomplete["world_id"]
        with self.assertRaisesRegex(ValueError, "missing keys"):
            self.command(CommandStatus.APPLIED, True, result=incomplete)
        wrong_type = self.world_commit_result()
        wrong_type["world_revision"] = "185"
        with self.assertRaisesRegex(ValueError, "world_revision must be an integer"):
            self.command(CommandStatus.APPLIED, True, result=wrong_type)
        with self.assertRaisesRegex(ValueError, "only APPLIED"):
            self.command(
                CommandStatus.FAILED,
                True,
                result=self.world_commit_result(),
                error=self.error,
            )
        created = {
            "result_type": "RESOURCE_CREATED",
            "resource_type": "SKILL_BUILD",
            "resource_id": "build_001",
            "resource_url": "/v1/skill-builds/build_001",
        }
        command = self.command(
            CommandStatus.APPLIED,
            True,
            result=created,
            command_type="CREATE_SKILL_BUILD",
        )
        self.assertEqual(command.result["resource_type"], "SKILL_BUILD")  # type: ignore[index]

    def test_command_uri_references_match_independent_jsonschema_formats(self) -> None:
        positive = (
            "../commands/cmd_python_0001",
            "/v1/commands/cmd_python_0001?view=full#result",
            "//api.example.test/v1/commands/cmd_python_0001",
            "https://api.example.test/v1/commands/cmd_python_0001",
            "resource%20name",
        )
        negative = (
            "%zz",
            "[",
            "a\\b",
            "a|b",
            "a{b}",
            "a^b",
            "://bad",
            "1http://x",
            "/v1/commands/😀",
        )
        checker = FormatChecker()
        if checker.conforms("%zz", "uri-reference"):
            self.fail("RFC3986 format validation is unavailable; install rfc3986-validator")
        for value in positive + negative:
            with self.subTest(value=value):
                expected = checker.conforms(value, "uri-reference")
                self.assertEqual(expected, _is_rfc3986_reference(value))

        base = self.command(CommandStatus.ACCEPTED, False)
        for value in positive:
            with self.subTest(field="links.self", value=value):
                self.assertEqual(replace(base, links={"self": value}).links["self"], value)
        for value in negative:
            with (
                self.subTest(field="links.self", value=value),
                self.assertRaisesRegex(ValueError, "RFC 3986 URI reference"),
            ):
                replace(base, links={"self": value})

        created = {
            "result_type": "RESOURCE_CREATED",
            "resource_type": "SKILL_BUILD",
            "resource_id": "build_001",
            "resource_url": "/v1/skill-builds/build_001",
        }
        created_command = self.command(
            CommandStatus.APPLIED,
            True,
            result=created,
            command_type="CREATE_SKILL_BUILD",
        )
        for value in negative:
            invalid_result = dict(created_command.result or {})
            invalid_result["resource_url"] = value
            with (
                self.subTest(field="result.resource_url", value=value),
                self.assertRaisesRegex(ValueError, "RFC 3986 URI reference"),
            ):
                replace(created_command, result=invalid_result)

    def test_llm_contract_pins_schema_limits_versions_and_degradation(self) -> None:
        schema = {"type": "object", "required": ["message"]}
        request = LlmRequest(
            messages=[LlmMessage("user", "Give a deterministic hint")],  # type: ignore[arg-type]
            output_schema=schema,
            temperature=0,
            max_output_tokens=256,
            timeout_ms=5_000,
            versions=self.versions,
        )
        schema["required"].append("mutated")  # type: ignore[union-attr]
        self.assertEqual(request.output_schema["required"], ("message",))
        self.assertIsInstance(request.messages, tuple)
        provider_reply = LlmReply(
            output={"message": "provider hint"},
            provider="openai",
            model="model-v1",
            source="provider",
            degraded=False,
            fallback_reason=None,
            input_tokens=10,
            output_tokens=5,
            evidence_refs=(),
        )
        fallback_reply = LlmReply(
            output={"message": "deterministic hint"},
            provider="fallback",
            model="deterministic-v1",
            source="provider_fallback",
            degraded=True,
            fallback_reason="MODEL_OUTPUT_INVALID",
            input_tokens=0,
            output_tokens=0,
            evidence_refs=(),
        )
        self.assertEqual(provider_reply.source, "provider")
        self.assertEqual(fallback_reply.source, "provider_fallback")
        with self.assertRaisesRegex(ValueError, "fallback reason"):
            LlmReply(
                output={"message": "deterministic hint"},
                provider="fallback",
                model="deterministic-v1",
                source="provider_fallback",
                degraded=True,
                fallback_reason=None,
                input_tokens=0,
                output_tokens=0,
                evidence_refs=(),
            )
        with self.assertRaisesRegex(ValueError, "provider_fallback source"):
            LlmReply(
                output={"message": "mislabelled fallback"},
                provider="fallback",
                model="deterministic-v1",
                source="provider",
                degraded=True,
                fallback_reason="MODEL_OUTPUT_INVALID",
                input_tokens=0,
                output_tokens=0,
                evidence_refs=(),
            )
        with self.assertRaisesRegex(ValueError, "provider source"):
            LlmReply(
                output={"message": "mislabelled provider"},
                provider="openai",
                model="model-v1",
                source="provider_fallback",
                degraded=False,
                fallback_reason=None,
                input_tokens=10,
                output_tokens=5,
                evidence_refs=(),
            )
        with self.assertRaisesRegex(ValueError, "boolean"):
            LlmReply(
                output={"message": "wrong bool type"},
                provider="fallback",
                model="deterministic-v1",
                source="provider_fallback",
                degraded=1,  # type: ignore[arg-type]
                fallback_reason="MODEL_OUTPUT_INVALID",
                input_tokens=0,
                output_tokens=0,
                evidence_refs=(),
            )
        with self.assertRaisesRegex(ValueError, "fallback_reason"):
            LlmReply(
                output={"message": "empty reason"},
                provider="fallback",
                model="deterministic-v1",
                source="provider_fallback",
                degraded=True,
                fallback_reason="",
                input_tokens=0,
                output_tokens=0,
                evidence_refs=(),
            )

    def test_certification_evidence_cannot_hide_failed_or_malformed_tests(self) -> None:
        artifact = BuildArtifact(
            artifact_sha256="a" * 64,
            source_sha256="b" * 64,
            compiler_profile="YAYA_CPP20_SAFE_V1",
            compiler_version="clang-20",
            sandbox_image_digest="sha256:" + "c" * 64,
            test_suite_version="suite-1",
            artifact_uri="artifact://skill/test",
        )
        passed = TestCaseResult("public-1", "PUBLIC", "PASSED", 10, (), ())
        short_diagnostic = TestCaseResult(
            "compiler-note",
            "PUBLIC",
            "PASSED",
            1,
            ("x",),
            (),
        )
        self.assertEqual(short_diagnostic.diagnostic_codes, ("x",))
        failed = TestCaseResult("hidden-1", "HIDDEN", "FAILED", 10, ("ASSERTION_FAILED",), ())
        CertificationEvidence("build_test_0001", artifact, (passed,), True, ())
        CertificationEvidence("build_test_0001", artifact, (failed,), False, ())
        with self.assertRaisesRegex(ValueError, "agree"):
            CertificationEvidence("build_test_0001", artifact, (failed,), True, ())
        with self.assertRaisesRegex(ValueError, "visibility"):
            TestCaseResult("bad-visibility", "PRIVATE", "PASSED", 1, (), ())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "status"):
            TestCaseResult("bad-status", "PUBLIC", "OK", 1, (), ())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "EvidenceRef"):
            TestCaseResult("bad-evidence", "PUBLIC", "PASSED", 1, (), ("bad",))  # type: ignore[arg-type]

    def test_source_bundle_and_sandbox_usage_are_strict_cross_language_values(self) -> None:
        content = "int main() { return 0; }"
        import hashlib

        source_file = SkillSourceFile(
            path="main.cpp",
            content=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        bundle = SkillSourceBundle(entrypoint="main.cpp", files=(source_file,))
        self.assertEqual(bundle.language, "CPP20")
        self.assertEqual(SandboxUsage(1, 2, 3).peak_memory_bytes, 3)
        with self.assertRaisesRegex(ValueError, "content_sha256"):
            SkillSourceFile("main.cpp", content, "0" * 64)
        with self.assertRaisesRegex(ValueError, "entrypoint"):
            SkillSourceBundle(entrypoint="missing.cpp", files=(source_file,))
        too_many_files = tuple(
            SkillSourceFile(
                path=f"src/file_{index}.cpp",
                content="",
                content_sha256=hashlib.sha256(b"").hexdigest(),
            )
            for index in range(33)
        )
        with self.assertRaisesRegex(ValueError, "32 source files"):
            SkillSourceBundle(
                entrypoint="src/file_0.cpp",
                files=too_many_files,
            )
        multibyte_content = "芽" * 174_763
        multibyte_files = tuple(
            SkillSourceFile(
                path=f"src/multibyte_{index}.cpp",
                content=multibyte_content,
                content_sha256=hashlib.sha256(multibyte_content.encode("utf-8")).hexdigest(),
            )
            for index in range(2)
        )
        with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
            SkillSourceBundle(
                entrypoint="src/multibyte_0.cpp",
                files=multibyte_files,
            )

    def test_outbox_states_are_closed_and_delivery_receipts_are_reconcilable(self) -> None:
        created_at = datetime.now(UTC)
        payload = DeliveryPayload(
            delivery_id="outbox_message_0001",
            operation="FEISHU_REPORT_DRAFT",
            deduplication_key="delivery:req_python_0001",
            attempt=1,
            body=FeishuReportDraftBody(report_id="report_0001"),
        )
        base = {
            "message_id": "outbox_message_0001",
            "destination": "FEISHU_REPORT_DRAFT",
            "idempotency_key": "delivery:req_python_0001",
            "payload": payload,
            "created_at": created_at,
            "operation_context": self.operation_context,
        }
        pending = OutboxMessage(**base)
        self.assertEqual(pending.status, OutboxStatus.PENDING)
        self.assertEqual(
            pending.idempotency_scope,
            ("tenant_yaya", "FEISHU_REPORT_DRAFT", "delivery:req_python_0001"),
        )
        other_actor_context = replace(
            self.operation_context,
            actor=ActorRef(
                "tenant_yaya",
                "service_delivery_0002",
                ActorType.SERVICE,
                ("delivery:worker",),
            ),
            command_id="cmd_python_0002",
        )
        same_tenant_delivery = OutboxMessage(**{**base, "operation_context": other_actor_context})
        self.assertNotEqual(
            pending.operation_context.actor,
            same_tenant_delivery.operation_context.actor,
        )
        self.assertEqual(
            pending.idempotency_scope,
            same_tenant_delivery.idempotency_scope,
            "Outbox is the explicit tenant-level service-delivery exception",
        )
        sending = OutboxMessage(
            **base,
            status=OutboxStatus.SENDING,
            attempt=1,
            lease_id="worker-lease-1",
            lease_expires_at=created_at + timedelta(minutes=1),
        )
        self.assertEqual(sending.attempt, 1)
        receipt = DeliveryReceipt(
            delivery_id="outbox_message_0001",
            operation="FEISHU_REPORT_DRAFT",
            deduplication_key="delivery:req_python_0001",
            report_id="report_0001",
            remote_object_id="doccn_remote_0001",
            sent_at=created_at + timedelta(seconds=1),
            attempt=1,
        )
        sent = OutboxMessage(
            **base,
            status=OutboxStatus.SENT,
            attempt=1,
            delivery_receipt=receipt,
        )
        self.assertEqual(sent.delivery_receipt, receipt)
        retrying = OutboxMessage(
            **base,
            status=OutboxStatus.RETRYING,
            attempt=1,
            next_attempt_at=created_at + timedelta(minutes=2),
            last_error=self.unknown_error,
        )
        self.assertEqual(retrying.last_error, self.unknown_error)
        dead = OutboxMessage(
            **{**base, "payload": replace(payload, attempt=3)},
            status=OutboxStatus.DEAD_LETTER,
            attempt=3,
            last_error=self.unknown_error,
            dead_lettered_at=created_at + timedelta(minutes=3),
        )
        self.assertEqual(dead.status, OutboxStatus.DEAD_LETTER)
        with self.assertRaisesRegex(ValueError, "PENDING"):
            OutboxMessage(**base, attempt=1)
        with self.assertRaisesRegex(ValueError, "SENT"):
            OutboxMessage(
                **base,
                status=OutboxStatus.SENT,
                attempt=1,
                delivery_receipt=receipt,
                last_error=self.unknown_error,
            )
        self.assertEqual(payload.delivery_id, receipt.delivery_id)

    def test_world_atomic_commit_rejects_wrong_types_and_identity_drift(self) -> None:
        intent = WaterIntent(
            intent_id="intent_water_0001",
            actor_entity_id="avatar_0001",
            expected_world_revision=1,
            plot_id="plot_0001",
            amount_ml=100,
        )
        command = WorldCommand(
            run_id="run_world_0001",
            world_id="world_demo_001",
            expected_world_revision=1,
            world_rules_version="farm-rules-12",
            skill_ref=SkillRef("skill_water_001", "skillver_water_001", "a" * 64, "cert_water_001"),
            intents=(intent,),
        )
        with self.assertRaisesRegex(TypeError, "SkillRef"):
            replace(command, skill_ref="bad")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "at least one intent"):
            replace(command, intents=())
        with self.assertRaisesRegex(TypeError, "ActionIntent"):
            replace(command, intents=("bad",))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "expected_world_revision"):
            replace(command, intents=(replace(intent, expected_world_revision=2),))
        with self.assertRaisesRegex(ValueError, "duplicate intent_id"):
            replace(command, intents=(intent, replace(intent, plot_id="plot_0002")))
        event = UncommittedEvent(
            event_type="world.committed",
            event_version=1,
            producer="world_engine",
            trace_id=self.operation_context.trace_id,
            command_id=self.operation_context.command_id,
            correlation_id=self.operation_context.correlation_id,
            causation_id=self.operation_context.command_id,
            content_ref=self.operation_context.content_ref,
            payload={"world_id": "world_demo_001", "run_id": "run_world_0001"},
        )
        commit = WorldAtomicCommit("world:world_demo_001", 10, command, (event,), ())
        self.assertEqual(commit.expected_stream_sequence, 10)
        with self.assertRaisesRegex(TypeError, "WorldCommand"):
            WorldAtomicCommit("world:world_demo_001", 10, "bad", (event,), ())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "UncommittedEvent"):
            WorldAtomicCommit("world:world_demo_001", 10, command, ("bad",), ())  # type: ignore[arg-type]
        drifted = replace(event, trace_id="trace_drifted_0001")
        with self.assertRaisesRegex(ValueError, "operation identity"):
            WorldAtomicCommit("world:world_demo_001", 10, command, (event, drifted), ())

    def test_command_transition_is_revision_cas(self) -> None:
        current = self.command(CommandStatus.ACCEPTED, False)
        next_record = replace(
            current,
            revision=2,
            status=CommandStatus.VALIDATING,
            stage="VALIDATE",
            updated_at=current.updated_at + timedelta(seconds=1),
        )
        transition = CommandTransition(
            current,
            next_record,
        )
        self.assertEqual(transition.next_record.revision, 2)
        with self.assertRaisesRegex(ValueError, "advance exactly once"):
            CommandTransition(
                current,
                replace(next_record, revision=3),
            )
        with self.assertRaisesRegex(ValueError, "immutable command_type"):
            CommandTransition(
                current,
                replace(next_record, command_type="INGEST_CLIENT_EVENTS"),
            )
        with self.assertRaisesRegex(ValueError, "status transition"):
            CommandTransition(
                current,
                replace(
                    next_record,
                    status=CommandStatus.APPLIED,
                    stage="COMPLETE",
                    terminal=True,
                    result=self.world_commit_result(),
                ),
            )

    def test_command_record_rejects_nested_type_and_boolean_spoofing(self) -> None:
        rejected = self.command(
            CommandStatus.REJECTED,
            True,
            error=self.error,
            stage="WORLD_VALIDATE",
        )
        for field_name, value, message in (
            ("request_context", object(), "RequestContext"),
            ("error", "not-an-error", "ContractError"),
            ("versions", object(), "VersionSet"),
            ("evidence_refs", ("not-evidence",), "EvidenceRef"),
            ("terminal", 1, "boolean"),
        ):
            with (
                self.subTest(field_name=field_name),
                self.assertRaisesRegex(
                    (TypeError, ValueError),
                    message,
                ),
            ):
                replace(rejected, **{field_name: value})

    def test_sandbox_success_result_rejects_failed_or_untyped_payloads(self) -> None:
        now = datetime.now(UTC)
        with self.assertRaisesRegex(ValueError, "status must be SUCCEEDED"):
            SandboxRunResult(
                "run_python_0001",
                now,
                now,
                (),
                None,
                None,
                SandboxUsage(0, 0, 0),
                (),
                status="FAILED",  # type: ignore[arg-type]
                exit_code=0,
            )
        with self.assertRaisesRegex(TypeError, "ActionIntent"):
            SandboxRunResult(
                "run_python_0001",
                now,
                now,
                ("not-an-intent",),  # type: ignore[arg-type]
                None,
                None,
                SandboxUsage(0, 0, 0),
                (),
            )

    def test_atomic_world_receipt_binds_the_explicit_event_stream(self) -> None:
        event = self.event(
            "evt_python_00000021",
            1,
            stream_id="audit:unrelated",
        )
        events = EventAppendReceipt("audit:unrelated", 0, 1, (event,))
        world = WorldCommitReceipt(
            "world_demo_001",
            0,
            1,
            1,
            1,
            datetime.now(UTC),
            "a" * 64,
        )
        with self.assertRaisesRegex(ValueError, "stream_id"):
            WorldAtomicCommitReceipt("world:expected", world, events, ())

    def test_audit_record_is_redacted_and_query_is_bounded(self) -> None:
        record = AuditRecord(
            audit_id="audit_python_0001",
            occurred_at=datetime.now(UTC),
            operation="queryLearnerProjectionFromFeishu",
            outcome="ALLOWED",
            actor=self.actor,
            request_id=self.operation_context.request_id,
            correlation_id=self.operation_context.correlation_id,
            trace_id=self.operation_context.trace_id,
            resource_type="LEARNER_PROJECTION",
            resource_id="projection_0001",
            purpose="TEACHER_SUPPORT",
            subject_hash="a" * 64,
            evidence_ids=(),
            error_code=None,
            details={"field_count": 2},
        )
        self.assertTrue(record.redacted)
        with self.assertRaisesRegex(ValueError, "not in the contract catalog"):
            replace(record, error_code="SILENT_UNKNOWN_ERROR")
        AuditQuery(outcomes=("ALLOWED",), limit=100)
        with self.assertRaisesRegex(ValueError, "at most 1000"):
            AuditQuery(limit=1001)

    def test_action_intent_is_a_strict_discriminated_value(self) -> None:
        intent = WaterIntent(
            intent_id="intent_water_001",
            actor_entity_id="avatar_001",
            expected_world_revision=184,
            plot_id="plot_001",
            amount_ml=250,
        )
        self.assertEqual(intent.action_type, "WATER")
        with self.assertRaisesRegex(ValueError, "amount_ml"):
            WaterIntent(
                intent_id="intent_water_001",
                actor_entity_id="avatar_001",
                expected_world_revision=184,
                plot_id="plot_001",
                amount_ml=0,
            )

    def test_world_snapshot_uses_transport_field_names_and_deep_freezes_state(self) -> None:
        snapshot = WorldSnapshot(
            request_context=self.context,
            world_id="world_demo_001",
            revision=184,
            last_event_sequence=731,
            state_hash="e" * 64,
            generated_at=datetime.now(UTC),
            world_rules_version="farm-rules-12",
            state=self.world_state(),
        )
        self.assertEqual(snapshot.revision, 184)
        self.assertIsInstance(snapshot.state["inventory"], tuple)
        self.assertFalse(hasattr(snapshot, "world_revision"))

    def test_world_snapshot_rejects_invalid_nested_state(self) -> None:
        invalid_states: list[tuple[str, dict[str, object]]] = []
        missing_clock_field = self.world_state()
        del missing_clock_field["clock"]["tick"]  # type: ignore[index]
        invalid_states.append(("state.clock", missing_clock_field))
        invalid_position = self.world_state()
        invalid_position["avatar"]["position"]["x"] = "zero"  # type: ignore[index]
        invalid_states.append(("state.avatar.position.x", invalid_position))
        invalid_inventory = self.world_state()
        invalid_inventory["inventory"][0]["quantity"] = -1  # type: ignore[index]
        invalid_states.append(("state.inventory", invalid_inventory))
        invalid_plot = self.world_state()
        invalid_plot["plots"][0]["soil_state"] = "FLOODED"  # type: ignore[index]
        invalid_states.append(("soil_state", invalid_plot))
        invalid_agent = self.world_state()
        invalid_agent["agents"][0]["activity"] = "UNKNOWN"  # type: ignore[index]
        invalid_states.append(("activity", invalid_agent))

        for expected_message, state in invalid_states:
            with (
                self.subTest(expected_message=expected_message),
                self.assertRaisesRegex(
                    ValueError,
                    expected_message,
                ),
            ):
                WorldSnapshot(
                    request_context=self.context,
                    world_id="world_demo_001",
                    revision=184,
                    last_event_sequence=731,
                    state_hash="e" * 64,
                    generated_at=datetime.now(UTC),
                    world_rules_version="farm-rules-12",
                    state=state,
                )

    def test_event_append_receipt_enforces_stream_order_and_identity(self) -> None:
        first = self.event("evt_python_00000001", 11)
        second = self.event("evt_python_00000002", 12)
        receipt = EventAppendReceipt("world:demo", 10, 12, (first, second))
        self.assertEqual(receipt.next_sequence, 12)

        wrong_stream = self.event("evt_python_00000003", 11, stream_id="world:other")
        with self.assertRaisesRegex(ValueError, "stream_id"):
            EventAppendReceipt("world:demo", 10, 11, (wrong_stream,))
        gap = self.event("evt_python_00000004", 12)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            EventAppendReceipt("world:demo", 10, 12, (gap,))
        duplicate = self.event("evt_python_00000001", 12)
        with self.assertRaisesRegex(ValueError, "duplicate event_id"):
            EventAppendReceipt("world:demo", 10, 12, (first, duplicate))

    def test_runtime_event_union_matches_asyncapi_and_domain_events_remain_extensible(self) -> None:
        asyncapi_path = (
            PACKAGE_ROOT.parent / "contracts" / "asyncapi" / "runtime-events.asyncapi.json"
        )
        document = json.loads(asyncapi_path.read_text(encoding="utf-8"))
        asyncapi_types: set[str] = set()
        asyncapi_payload_fields: dict[str, set[str]] = {}
        for schema in document["components"]["schemas"].values():
            branches = schema.get("allOf")
            if not branches or len(branches) < 2:
                continue
            event_type = branches[1].get("properties", {}).get("event_type", {}).get("const")
            if event_type:
                asyncapi_types.add(event_type)
                payload_ref = branches[1]["properties"]["payload"]["$ref"]
                payload_name = payload_ref.rsplit("/", 1)[-1]
                payload_schema = document["components"]["schemas"][payload_name]
                asyncapi_payload_fields[event_type] = set(payload_schema["required"])
        self.assertEqual({event_type.value for event_type in RuntimeEventType}, asyncapi_types)
        self.assertEqual(len(asyncapi_types), 25)

        def sample_payload(event_type: str, required_fields: set[str]) -> dict[str, object]:
            identifier_fields = {
                "build_id",
                "skill_id",
                "worker_id",
                "certification_id",
                "skill_version_id",
                "run_id",
                "world_id",
                "commit_id",
                "learner_id",
                "sync_id",
                "session_id",
                "turn_id",
            }
            array_fields = {
                "capabilities",
                "evidence_refs",
                "action_intents",
                "applied_intent_ids",
                "rejected_intent_ids",
                "competency_ids",
                "changed_competency_ids",
            }
            error = {
                "code": "INTERNAL_ERROR",
                "category": "INTERNAL",
                "retryable": False,
                "user_message_key": "system.internal_error",
                "stage": "INTERNAL",
            }
            artifact = {
                "artifact_sha256": "a" * 64,
                "source_sha256": "b" * 64,
                "compiler_profile": "YAYA_CPP20_SAFE_V1",
                "compiler_version": "clang-18",
                "sandbox_image_digest": "sha256:image",
                "test_suite_version": "suite-1",
                "artifact_uri": "/artifacts/build-1",
            }
            values: dict[str, object] = {}
            for field in required_fields:
                if field in identifier_fields:
                    values[field] = "resource_00000001"
                elif field.endswith("_at"):
                    values[field] = "2026-08-07T10:00:00Z"
                elif field in array_fields:
                    values[field] = []
                elif field in {"source_sha256", "artifact_sha256", "state_hash"}:
                    values[field] = "a" * 64
                elif field == "error":
                    values[field] = error
                elif field == "artifact":
                    values[field] = artifact
                elif field == "tests":
                    values[field] = [
                        {
                            "test_case_id": "test-1",
                            "visibility": "PUBLIC",
                            "status": "PASSED",
                            "duration_ms": 1,
                            "diagnostic_codes": [],
                            "evidence_refs": [],
                        }
                    ]
                elif field == "activation_scope":
                    values[field] = {
                        "world_id": "world_00000001",
                        "agent_profile_id": "profile_00000001",
                    }
                elif field == "next_attempt_at":
                    values[field] = None
                elif field == "result_ref":
                    values[field] = "/v1/commands/cmd_python_0001"
                elif field == "command_type":
                    values[field] = "EXECUTE_AGENT_TURN"
                elif field == "command_id":
                    values[field] = "cmd_runtime_00000001"
                elif field == "message_key":
                    values[field] = "agent.turn.completed"
                elif field == "message":
                    values[field] = "The requested turn is complete."
                elif field == "source":
                    values[field] = "provider"
                elif field == "degraded":
                    values[field] = False
                elif field == "fallback_reason":
                    values[field] = None
                elif field in {"from_status", "to_status"}:
                    values[field] = "ACCEPTED"
                elif field == "status":
                    values[field] = "ACCEPTED"
                elif field == "sync_kind":
                    values[field] = "REPORT_DRAFT"
                elif field == "source_event_id":
                    values[field] = "evt_source_00000001"
                elif field == "exit_code":
                    values[field] = 0
                elif field in {
                    "compiler_profile",
                    "test_suite_version",
                    "target_ref",
                    "remote_object_id",
                }:
                    values[field] = "contract-value"
                else:
                    values[field] = 1
            if event_type == "command.terminal":
                values.update({"status": "APPLIED", "error": None})
            if event_type == "command.stage_changed":
                values.update({"from_status": "ACCEPTED", "to_status": "VALIDATING"})
            if event_type == "world.committed":
                values.update({"previous_world_revision": 1, "world_revision": 2})
            if event_type == "skill.activation.applied":
                values.update({"previous_registry_revision": 1, "registry_revision": 2})
            if event_type == "learner.model.updated":
                values.update({"previous_revision": 1, "learner_revision": 2})
            if event_type == "agent.turn.feedback_ready":
                values["command_id"] = "cmd_python_0001"
            if event_type == "learner.inference.recorded":
                values.update(
                    {
                        "actor": {
                            "tenant_id": "tenant_yaya",
                            "actor_id": "resource_00000001",
                            "actor_type": "student",
                            "roles": ["game:player"],
                        },
                        "learner_id": "resource_00000001",
                        "session_id": "session_runtime_00000001",
                        "turn_id": "turn_runtime_00000001",
                        "command_id": "cmd_runtime_00000001",
                        "run_id": "run_runtime_00000001",
                        "source_event_id": "evt_source_00000001",
                        "source_event_sha256": "a" * 64,
                        "turn_commit_sha256": "b" * 64,
                        "task_id": "task_runtime_00000001",
                        "teaching_spec_version": "teaching-7",
                        "role": "teaching_agent",
                        "concept": "loops.for",
                        "score_delta": 0.2,
                        "confidence": 0.8,
                        "reason": "The learner completed the task with validated evidence.",
                        "evidence_refs": [
                            {
                                "evidence_id": "evidence_runtime_00000001",
                                "evidence_type": "TEST_REPORT",
                                "created_at": "2026-08-07T10:00:00Z",
                                "sha256": "c" * 64,
                            }
                        ],
                    }
                )
                values["inference_sha256"] = learner_inference_sha256(values)
            return values

        for index, (event_type, required_fields) in enumerate(
            sorted(asyncapi_payload_fields.items())
        ):
            with self.subTest(event_type=event_type):
                payload = sample_payload(event_type, required_fields)
                if event_type == "learner.inference.recorded":
                    runtime_event = self.event(
                        f"evt_runtime_{index:08d}",
                        1,
                        stream_id=f"learner:{payload['learner_id']}",
                        event_type=event_type,
                        payload=payload,
                        event_class=RuntimeEvent,
                        schema_version="2.0.0",
                        command_id=str(payload["command_id"]),
                        causation_id=str(payload["source_event_id"]),
                    )
                else:
                    runtime_event = self.event(
                        f"evt_runtime_{index:08d}",
                        1,
                        stream_id="runtime:python",
                        event_type=event_type,
                        payload=payload,
                        event_class=RuntimeEvent,
                    )
                self.assertEqual(str(runtime_event.event_type), event_type)

        runtime = self.event(
            "evt_python_00000010",
            1,
            stream_id="command:python",
            event_type="command.accepted",
            payload={
                "command_type": "EXECUTE_AGENT_TURN",
                "status": "ACCEPTED",
                "accepted_at": "2026-08-07T10:00:00Z",
            },
            event_class=RuntimeEvent,
        )
        self.assertEqual(runtime.event_type, RuntimeEventType.COMMAND_ACCEPTED)
        with self.assertRaisesRegex(ValueError, "event_version"):
            self.event(
                "evt_python_00000014",
                1,
                event_type="command.accepted",
                payload={
                    "command_type": "EXECUTE_AGENT_TURN",
                    "status": "ACCEPTED",
                    "accepted_at": "2026-08-07T10:00:00Z",
                },
                event_class=RuntimeEvent,
                event_version=2,
            )
        with self.assertRaisesRegex(ValueError, "command_type or status"):
            self.event(
                "evt_python_00000015",
                1,
                event_type="command.accepted",
                payload={
                    "command_type": None,
                    "status": None,
                    "accepted_at": "2026-08-07T10:00:00Z",
                },
                event_class=RuntimeEvent,
            )
        with self.assertRaisesRegex(ValueError, "UNKNOWN_COMMIT_STATE"):
            self.event(
                "evt_python_00000016",
                1,
                event_type="command.terminal",
                payload={
                    "status": "UNKNOWN",
                    "terminal_at": "2026-08-07T10:00:00Z",
                    "result_ref": None,
                    "error": {
                        "code": "DEPENDENCY_UNAVAILABLE",
                        "category": "DEPENDENCY",
                        "retryable": True,
                        "user_message_key": "dependency.temporarily_unavailable",
                        "stage": "WORLD_COMMIT",
                    },
                },
                event_class=RuntimeEvent,
            )
        invalid_world_commit = sample_payload(
            "world.committed",
            asyncapi_payload_fields["world.committed"],
        )
        invalid_world_commit.update({"previous_world_revision": 7, "world_revision": 9})
        with self.assertRaisesRegex(ValueError, "advance exactly one world revision"):
            self.event(
                "evt_python_00000017",
                1,
                event_type="world.committed",
                payload=invalid_world_commit,
                event_class=RuntimeEvent,
            )
        with self.assertRaisesRegex(ValueError, "unknown runtime event_type"):
            self.event(
                "evt_python_00000011",
                1,
                event_type="world.plot_watered",
                event_class=RuntimeEvent,
            )
        with self.assertRaisesRegex(ValueError, "must change status"):
            self.event(
                "evt_python_00000022",
                1,
                stream_id="runtime:python",
                event_type="command.stage_changed",
                payload={
                    "from_status": "ACCEPTED",
                    "to_status": "ACCEPTED",
                    "command_revision": 2,
                    "attempt": 1,
                },
                event_class=RuntimeEvent,
            )
        with self.assertRaisesRegex(ValueError, "missing keys"):
            self.event(
                "evt_python_00000012",
                1,
                event_type="command.accepted",
                payload={"command_type": "EXECUTE_AGENT_TURN"},
                event_class=RuntimeEvent,
            )
        domain_event = self.event(
            "evt_python_00000013",
            1,
            event_type="world.plot_watered",
            payload={"plot_id": "plot_001"},
        )
        self.assertEqual(domain_event.event_type, "world.plot_watered")

    def test_command_acceptance_receipt_distinguishes_create_from_replay(self) -> None:
        command = self.command(
            CommandStatus.APPLIED,
            True,
            result=self.world_commit_result(),
        )
        self.assertTrue(CommandCreateReceipt(command=command, created=True).created)
        self.assertFalse(CommandCreateReceipt(command=command, created=False).created)
        with self.assertRaisesRegex(ValueError, "created"):
            CommandCreateReceipt(command=command, created=1)  # type: ignore[arg-type]
        annotation = inspect.signature(CommandStorePort.accept_once).return_annotation
        self.assertIn("CommandCreateReceipt", str(annotation))

    def test_rfc3339_datetime_validation_matches_json_schema_format(self) -> None:
        positive = (
            "2026-08-07T10:00:00Z",
            "2026-08-07t10:00:00z",
            "2026-08-07T10:00:00+08:00",
            "2026-08-07T10:00:00-00:00",
            "2024-02-29T23:59:59.123456789-03:30",
        )
        negative = (
            "20260807T100000Z",
            "2026-08-07 10:00:00Z",
            "2026-08-07T10:00Z",
            "2026-08-07T10:00:00",
            "2026-02-29T10:00:00Z",
            "2026-13-07T10:00:00Z",
            "2026-08-32T10:00:00Z",
            "2026-08-07T24:00:00Z",
            "2026-08-07T23:59:60Z",
            "2026-08-07T10:00:00+24:00",
            "2026-08-07T10:00:00+23:60",
            "2026-08-07T10:00:00+0800",
            "2026-08-07T10:00:00+08:00:30",
            "0000-01-01T00:00:00Z",
            "２０２６-08-07T10:00:00Z",
        )
        checker = FormatChecker()
        if checker.conforms("not-a-date-time", "date-time"):
            self.fail("RFC3339 format validation is unavailable; install rfc3339-validator")
        for value in positive:
            with self.subTest(value=value):
                self.assertTrue(checker.conforms(value, "date-time"))
                _require_iso_datetime(value, "timestamp")
        for value in negative:
            with self.subTest(value=value):
                self.assertFalse(checker.conforms(value, "date-time"))
                with self.assertRaisesRegex(ValueError, "RFC 3339"):
                    _require_iso_datetime(value, "timestamp")
        with self.assertRaisesRegex(ValueError, "RFC 3339"):
            _require_iso_datetime(datetime.now(UTC), "timestamp")

    def test_new_command_materializes_complete_initial_record(self) -> None:
        new_command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key="turn:req_python_0001",
            request_sha256="b" * 64,
            versions=self.versions,
        )
        accepted_at = datetime.now(UTC)
        record = new_command.initial_record(self.operation_context, accepted_at)

        self.assertEqual(
            new_command.idempotency_scope(self.operation_context),
            (
                "tenant_yaya",
                "student_0001",
                "EXECUTE_AGENT_TURN",
                "turn:req_python_0001",
            ),
        )
        self.assertEqual(record.command_id, self.operation_context.command_id)
        self.assertEqual(record.request_context.request_id, self.operation_context.request_id)
        self.assertEqual(record.request_context.actor, self.operation_context.actor)
        self.assertEqual(record.command_type, "EXECUTE_AGENT_TURN")
        self.assertIs(record.status, CommandStatus.ACCEPTED)
        self.assertEqual(record.stage, "ACCEPT")
        self.assertFalse(record.terminal)
        self.assertEqual(record.accepted_at, accepted_at)
        self.assertEqual(record.updated_at, accepted_at)
        self.assertIsNone(record.result)
        self.assertIsNone(record.error)
        self.assertEqual(record.evidence_refs, ())
        self.assertEqual(record.links, {"self": "/v1/commands/cmd_python_0001"})

        input_fields = {item.name for item in fields(NewCommand)}
        self.assertEqual(
            input_fields,
            {"command_type", "idempotency_key", "request_sha256", "versions"},
        )
        self.assertTrue(
            {
                "command_id",
                "request_context",
                "accepted_at",
                "updated_at",
                "status",
                "stage",
            }.isdisjoint(input_fields)
        )
        with self.assertRaises(FrozenInstanceError):
            new_command.command_type = "CREATE_AGENT_SESSION"  # type: ignore[misc]

    def test_new_command_idempotency_scope_is_actor_bound(self) -> None:
        new_command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key="turn:req_python_0001",
            request_sha256="b" * 64,
            versions=self.versions,
        )
        other_actor_context = replace(
            self.operation_context,
            actor=ActorRef(
                "tenant_yaya",
                "student_0002",
                ActorType.STUDENT,
                ("game:player",),
            ),
            command_id="cmd_python_0002",
        )

        self.assertEqual(
            new_command.idempotency_scope(self.operation_context),
            (
                "tenant_yaya",
                "student_0001",
                "EXECUTE_AGENT_TURN",
                "turn:req_python_0001",
            ),
        )
        self.assertNotEqual(
            new_command.idempotency_scope(self.operation_context),
            new_command.idempotency_scope(other_actor_context),
        )

    def test_new_command_rejects_invalid_idempotency_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "command_type"):
            NewCommand("UNKNOWN", "turn:req_python_0001", "b" * 64, self.versions)
        with self.assertRaisesRegex(ValueError, "idempotency_key"):
            NewCommand("EXECUTE_AGENT_TURN", "too-short", "b" * 64, self.versions)
        with self.assertRaisesRegex(ValueError, "request_sha256"):
            NewCommand(
                "EXECUTE_AGENT_TURN",
                "turn:req_python_0001",
                "not-a-sha256",
                self.versions,
            )
        with self.assertRaisesRegex(ValueError, "accepted_at"):
            NewCommand(
                "EXECUTE_AGENT_TURN",
                "turn:req_python_0001",
                "b" * 64,
                self.versions,
            ).initial_record(self.operation_context, datetime.now())

    def test_command_store_acceptance_signature_is_atomic_and_scoped(self) -> None:
        signature = inspect.signature(CommandStorePort.accept_once)
        self.assertEqual(list(signature.parameters), ["self", "command", "context"])
        self.assertIn("NewCommand", str(signature.parameters["command"].annotation))
        self.assertIn("CommandCreateReceipt", str(signature.return_annotation))

        lookup_signature = inspect.signature(CommandStorePort.get_by_idempotency_key)
        self.assertEqual(
            list(lookup_signature.parameters),
            ["self", "operation", "idempotency_key", "context"],
        )
        self.assertEqual(
            lookup_signature.parameters["operation"].annotation,
            "CommandType",
        )
        self.assertIn("EXECUTE_AGENT_TURN", str(CommandType.__value__))

    def test_every_port_operation_propagates_operation_context(self) -> None:
        ports = (
            AuditPort,
            PolicyPort,
            RegistryPort,
            SandboxPort,
            WorldPort,
            WorldUnitOfWorkPort,
            EventStorePort,
            LearnerPort,
            LlmPort,
            DeliveryPort,
            CommandStorePort,
            OutboxPort,
        )
        for port in ports:
            for name, member in vars(port).items():
                if name.startswith("_") or not inspect.isfunction(member):
                    continue
                with self.subTest(port=port.__name__, method=name):
                    self.assertIn("context", inspect.signature(member).parameters)

    def test_python_ports_exactly_match_the_frozen_cross_language_surface(self) -> None:
        def normalized_type(annotation: object) -> str:
            return re.sub(
                r"\s*([\[\](){},:<>|])\s*",
                r"\1",
                str(annotation).strip(),
            )

        manifest = json.loads(
            (Path(__file__).resolve().parents[1] / "contracts" / "port-surface.json").read_text(
                encoding="utf-8"
            )
        )
        for port_contract in manifest["ports"]:
            port = getattr(ports_module, port_contract["python"])
            expected_methods = {method["python"] for method in port_contract["methods"]}
            actual_methods = {
                name
                for name, member in vars(port).items()
                if not name.startswith("_") and inspect.isfunction(member)
            }
            with self.subTest(port=port.__name__):
                self.assertEqual(actual_methods, expected_methods)
            for method_contract in port_contract["methods"]:
                member = getattr(port, method_contract["python"])
                signature = inspect.signature(member)
                parameters = list(signature.parameters)
                frozen = method_contract["python_contract"]
                self.assertEqual(
                    parameters,
                    ["self", *[item["name"] for item in frozen["parameters"]]],
                    f"{port.__name__}.{method_contract['python']} parameter drift",
                )
                self.assertEqual(
                    inspect.iscoroutinefunction(member),
                    frozen["is_async"],
                    f"{port.__name__}.{method_contract['python']} async drift",
                )
                for parameter_contract in frozen["parameters"]:
                    self.assertEqual(
                        normalized_type(
                            signature.parameters[parameter_contract["name"]].annotation
                        ),
                        parameter_contract["type"],
                        f"{port.__name__}.{method_contract['python']} "
                        f"{parameter_contract['name']} type drift",
                    )
                self.assertEqual(
                    normalized_type(signature.return_annotation),
                    frozen["return_type"],
                    f"{port.__name__}.{method_contract['python']} return type drift",
                )
        learner_project = inspect.signature(LearnerPort.project)
        self.assertEqual(learner_project.parameters["event"].annotation, "RuntimeEvent")

    def test_missing_capabilities_are_present_and_vendor_alias_is_compatible(self) -> None:
        self.assertTrue(hasattr(WorldUnitOfWorkPort, "commit"))
        self.assertTrue(hasattr(EventStorePort, "read_stream"))
        self.assertTrue(hasattr(EventStorePort, "get_by_id"))
        self.assertTrue(hasattr(RegistryPort, "certify"))
        self.assertTrue(hasattr(SandboxPort, "cancel"))
        self.assertTrue(hasattr(OutboxPort, "claim_ready"))
        self.assertTrue(hasattr(OutboxPort, "mark_dead_letter"))
        self.assertIs(FeishuPort, DeliveryPort)


if __name__ == "__main__":
    unittest.main()
