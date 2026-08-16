from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = AGENT_ROOT / "contracts"
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
EXAMPLE_ROOT = CONTRACT_ROOT / "examples"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class StandardSchemaValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema_files = sorted(SCHEMA_ROOT.rglob("*.schema.json"))
        cls.schemas = {path: read_json(path) for path in cls.schema_files}
        registry = Registry()
        for contract_path in sorted(CONTRACT_ROOT.rglob("*.json")):
            document = read_json(contract_path)
            resource = Resource(contents=document, specification=DRAFT202012)
            registry = registry.with_resource(contract_path.as_uri(), resource)
            if "$id" in document:
                registry = registry.with_resource(document["$id"], resource)
        cls.registry = registry

    @staticmethod
    def local_ref(source_path: Path, reference: str) -> str:
        file_part, separator, fragment = reference.partition("#")
        target_path = (source_path.parent / file_part).resolve()
        return f"{target_path.as_uri()}#{fragment}" if separator else target_path.as_uri()

    def assert_valid(self, value: object, schema_path: Path) -> None:
        schema = self.schemas[schema_path]
        validator_type = validator_for(schema)
        validator_type.check_schema(schema)
        validator = validator_type(
            schema,
            registry=self.registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        self.assertEqual([], [error.message for error in errors])

    def assert_invalid(self, value: object, schema_path: Path) -> None:
        schema = self.schemas[schema_path]
        validator_type = validator_for(schema)
        validator = validator_type(
            schema,
            registry=self.registry,
            format_checker=FormatChecker(),
        )
        self.assertTrue(
            list(validator.iter_errors(value)), "mutation unexpectedly passed standard JSON Schema"
        )

    def errors_for_ref(self, value: object, schema_ref: str) -> list:
        wrapper = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": schema_ref,
        }
        validator_type = validator_for(wrapper)
        validator = validator_type(
            wrapper,
            registry=self.registry,
            format_checker=FormatChecker(),
        )
        return list(validator.iter_errors(value))

    def errors_for_local_ref(
        self,
        value: object,
        source_path: Path,
        schema_ref: str,
    ) -> list:
        return self.errors_for_ref(value, self.local_ref(source_path, schema_ref))

    def example(self, name: str) -> tuple[dict, Path]:
        wrapper_path = EXAMPLE_ROOT / name
        wrapper = read_json(wrapper_path)
        schema_path = (wrapper_path.parent / wrapper["schema_ref"]).resolve()
        return wrapper["value"], schema_path

    def test_all_schemas_compile_and_examples_validate(self) -> None:
        self.assertGreaterEqual(len(self.schema_files), 30)
        for schema_path, schema in self.schemas.items():
            with self.subTest(schema=schema_path.name):
                validator_for(schema).check_schema(schema)
        for example_path in sorted(EXAMPLE_ROOT.glob("*.json")):
            with self.subTest(example=example_path.name):
                wrapper = read_json(example_path)
                self.assertEqual(
                    [],
                    [
                        error.message
                        for error in self.errors_for_local_ref(
                            wrapper["value"],
                            example_path,
                            wrapper["schema_ref"],
                        )
                    ],
                )

    def test_realtime_control_frames_are_closed_under_standard_validation(self) -> None:
        names = [
            "realtime-subscribe-frame.json",
            "realtime-resume-frame.json",
            "realtime-ack-frame.json",
            "realtime-heartbeat-ack-frame.json",
            "realtime-subscribed-frame.json",
            "realtime-heartbeat-frame.json",
            "realtime-error-frame.json",
        ]
        for name in names:
            path = EXAMPLE_ROOT / name
            wrapper = read_json(path)
            with self.subTest(frame=name, mutation="valid"):
                self.assertEqual(
                    [],
                    self.errors_for_local_ref(wrapper["value"], path, wrapper["schema_ref"]),
                )
            missing = deepcopy(wrapper["value"])
            missing.pop("frame_type")
            with self.subTest(frame=name, mutation="missing frame_type"):
                self.assertTrue(self.errors_for_local_ref(missing, path, wrapper["schema_ref"]))
            extra = deepcopy(wrapper["value"])
            extra["ignored_typo"] = True
            with self.subTest(frame=name, mutation="unknown field"):
                self.assertTrue(self.errors_for_local_ref(extra, path, wrapper["schema_ref"]))

        error_path = EXAMPLE_ROOT / "realtime-error-frame.json"
        error_wrapper = read_json(error_path)
        fatal_without_close = deepcopy(error_wrapper["value"])
        fatal_without_close["close_code"] = None
        self.assertTrue(
            self.errors_for_local_ref(
                fatal_without_close,
                error_path,
                error_wrapper["schema_ref"],
            )
        )
        non_fatal_with_close = deepcopy(error_wrapper["value"])
        non_fatal_with_close["fatal"] = False
        self.assertTrue(
            self.errors_for_local_ref(
                non_fatal_with_close,
                error_path,
                error_wrapper["schema_ref"],
            )
        )
        mismatched_close = deepcopy(error_wrapper["value"])
        mismatched_close["close_code"] = 4401
        self.assertTrue(
            self.errors_for_local_ref(
                mismatched_close,
                error_path,
                error_wrapper["schema_ref"],
            )
        )
        non_retryable_delay = deepcopy(error_wrapper["value"])
        non_retryable_delay["close_code"] = 4401
        non_retryable_delay["retry_after_ms"] = 1000
        non_retryable_delay["error"] = {
            "code": "AUTHENTICATION_REQUIRED",
            "category": "AUTHENTICATION",
            "retryable": False,
            "user_message_key": "auth.login_required",
            "stage": "REALTIME_HANDSHAKE",
        }
        self.assertTrue(
            self.errors_for_local_ref(
                non_retryable_delay,
                error_path,
                error_wrapper["schema_ref"],
            )
        )

    def test_existing_conditional_contracts_reject_false_terminal_states(self) -> None:
        skill_build, schema_path = self.example("game-skill-build.json")
        mutation = deepcopy(skill_build)
        mutation["status"] = "CERTIFIED"
        mutation["terminal"] = False
        self.assert_invalid(mutation, schema_path)

    def test_webhook_quarantine_requires_a_reason(self) -> None:
        receipt, schema_path = self.example("feishu-webhook-response.json")
        mutation = deepcopy(receipt)
        mutation["disposition"] = "QUARANTINED_UNSUPPORTED"
        mutation.pop("quarantine_reason", None)
        self.assert_invalid(mutation, schema_path)

    def test_feishu_response_trace_ids_reject_noncanonical_projections(self) -> None:
        names = [
            "feishu-approval-decision-response.json",
            "feishu-class-insights-response.json",
            "feishu-content-release-response.json",
            "feishu-webhook-response.json",
            "feishu-learner-query-response.json",
            "feishu-evidence-response.json",
        ]
        for name in names:
            value, schema_path = self.example(name)
            with self.subTest(example=name, trace_id="canonical"):
                self.assert_valid(value, schema_path)
            for invalid in ("bad", "trace_short", "trace_has.dot_0001", f"trace_{'a' * 97}"):
                mutation = deepcopy(value)
                mutation["trace_id"] = invalid
                with self.subTest(example=name, trace_id=invalid):
                    self.assert_invalid(mutation, schema_path)

    def test_command_state_machine_rejects_contradictory_terminal_states(self) -> None:
        command, schema_path = self.example("game-command.json")

        missing_result = deepcopy(command)
        missing_result["result"] = None
        self.assert_invalid(missing_result, schema_path)

        premature_result = deepcopy(command)
        premature_result["status"] = "RUNNING_SANDBOX"
        premature_result["stage"] = "SANDBOX"
        premature_result["terminal"] = False
        self.assert_invalid(premature_result, schema_path)

        unknown_not_terminal = deepcopy(command)
        unknown_not_terminal["status"] = "UNKNOWN"
        unknown_not_terminal["stage"] = "WORLD_COMMIT"
        unknown_not_terminal["terminal"] = False
        unknown_not_terminal["result"] = None
        unknown_not_terminal["error"] = {
            "code": "UNKNOWN_COMMIT_STATE",
            "category": "DEPENDENCY",
            "retryable": False,
            "user_message_key": "command.reconciling",
            "stage": "WORLD_COMMIT",
        }
        self.assert_invalid(unknown_not_terminal, schema_path)

    def test_run_and_report_job_state_machines_reject_mixed_phases(self) -> None:
        run, run_schema = self.example("game-run.json")
        running_sandbox = deepcopy(run)
        running_sandbox["sandbox"]["status"] = "RUNNING"
        self.assert_invalid(running_sandbox, run_schema)

        false_terminal = deepcopy(run)
        false_terminal["terminal"] = False
        self.assert_invalid(false_terminal, run_schema)

        feedback_without_run_identity = deepcopy(run)
        feedback_without_run_identity["agent_feedback"]["run_id"] = None
        self.assert_invalid(feedback_without_run_identity, run_schema)

        report, report_schema = self.example("feishu-report-job-response.json")
        finished_but_not_started = deepcopy(report)
        finished_but_not_started["job"]["status"] = "SUCCEEDED"
        self.assert_invalid(finished_but_not_started, report_schema)

    def test_error_schema_rejects_unknown_codes_and_catalog_tuple_drift(self) -> None:
        command, schema_path = self.example("game-command.json")
        command["status"] = "UNKNOWN"
        command["stage"] = "WORLD_COMMIT"
        command["result"] = None
        command["error"] = {
            "code": "UNKNOWN_COMMIT_STATE",
            "category": "DEPENDENCY",
            "retryable": False,
            "user_message_key": "command.reconciling",
            "stage": "WORLD_COMMIT",
        }
        self.assert_valid(command, schema_path)

        unknown_code = deepcopy(command)
        unknown_code["error"]["code"] = "SILENT_NEW_ERROR"
        self.assert_invalid(unknown_code, schema_path)

        wrong_retry_semantics = deepcopy(command)
        wrong_retry_semantics["error"]["retryable"] = True
        self.assert_invalid(wrong_retry_semantics, schema_path)

    def test_unknown_commit_state_is_bound_in_run_and_error_response(self) -> None:
        run, run_schema = self.example("game-run.json")
        run["status"] = "UNKNOWN"
        run["terminal"] = True
        run["world_application"] = {
            "status": "UNKNOWN",
            "receipt": None,
            "failure": {
                "code": "UNKNOWN_COMMIT_STATE",
                "category": "DEPENDENCY",
                "retryable": False,
                "user_message_key": "command.reconciling",
                "stage": "WORLD_COMMIT",
            },
        }
        self.assert_valid(run, run_schema)
        wrong_run = deepcopy(run)
        wrong_run["world_application"]["failure"] = {
            "code": "DEPENDENCY_UNAVAILABLE",
            "category": "DEPENDENCY",
            "retryable": True,
            "user_message_key": "dependency.temporarily_unavailable",
            "stage": "WORLD_COMMIT",
        }
        self.assert_invalid(wrong_run, run_schema)

        error_response_schema = SCHEMA_ROOT / "common" / "error-response.schema.json"
        response = {
            "request_id": "req_contract_0001",
            "command_id": "cmd_contract_0001",
            "trace_id": "trace_contract_0001",
            "status": "UNKNOWN",
            "data": None,
            "error": deepcopy(run["world_application"]["failure"]),
        }
        self.assert_valid(response, error_response_schema)

        missing_command_id = deepcopy(response)
        missing_command_id.pop("command_id")
        self.assert_invalid(missing_command_id, error_response_schema)

        wrong_unknown_code = deepcopy(response)
        wrong_unknown_code["error"] = deepcopy(wrong_run["world_application"]["failure"])
        self.assert_invalid(wrong_unknown_code, error_response_schema)

        unknown_code_with_failed_status = deepcopy(response)
        unknown_code_with_failed_status["status"] = "FAILED"
        self.assert_invalid(unknown_code_with_failed_status, error_response_schema)

        ordinary_failure = deepcopy(unknown_code_with_failed_status)
        ordinary_failure.pop("command_id")
        ordinary_failure["error"] = deepcopy(wrong_run["world_application"]["failure"])
        self.assert_valid(ordinary_failure, error_response_schema)

    def test_error_response_code_is_bound_to_http_status_schema(self) -> None:
        catalog = read_json(CONTRACT_ROOT / "error-catalog.json")
        entries = {entry["code"]: entry for entry in catalog["errors"]}

        def response(code: str) -> dict:
            entry = entries[code]
            return {
                "request_id": "req_contract_0001",
                "trace_id": "trace_contract_0001",
                "status": "FAILED",
                "data": None,
                "error": {
                    "code": entry["code"],
                    "category": entry["category"],
                    "retryable": entry["retryable"],
                    "user_message_key": entry["user_message_key"],
                    "stage": "HTTP_ADAPTER",
                },
            }

        base = "https://contracts.yaya.local/common/error-responses-by-status.schema.json#/$defs/"
        self.assertEqual([], self.errors_for_ref(response("INVALID_REQUEST"), f"{base}status400"))
        self.assertTrue(self.errors_for_ref(response("INTERNAL_ERROR"), f"{base}status400"))
        self.assertEqual([], self.errors_for_ref(response("INTERNAL_ERROR"), f"{base}status500"))
        self.assertTrue(self.errors_for_ref(response("INVALID_REQUEST"), f"{base}status500"))

    def test_public_uri_references_are_non_empty_and_bounded(self) -> None:
        for example_name, path in [
            ("game-command.json", ("links", "self")),
            ("game-agent-session.json", ("links", "self")),
            ("game-bootstrap-response.json", ("world", "snapshot_url")),
        ]:
            value, schema_path = self.example(example_name)
            for invalid in ("", "x" * 2049):
                mutation = deepcopy(value)
                target = mutation
                for segment in path[:-1]:
                    target = target[segment]
                target[path[-1]] = invalid
                with self.subTest(example=example_name, invalid_length=len(invalid)):
                    self.assert_invalid(mutation, schema_path)


if __name__ == "__main__":
    unittest.main()
