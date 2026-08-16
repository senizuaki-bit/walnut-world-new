from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = AGENT_ROOT / "python"
CONTRACTS_ROOT = AGENT_ROOT / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ContentRef,
    RuntimeEvent,
    RuntimeEventType,
    learner_inference_sha256,
)


class LearnerInferenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        wrapper = json.loads(
            (CONTRACTS_ROOT / "examples" / "runtime-learner-inference-recorded.json").read_text(
                encoding="utf-8"
            )
        )
        cls.example: dict[str, Any] = wrapper["value"]
        cls.schema_validator = ContractSchemaValidator(CONTRACTS_ROOT)

    def runtime_event(
        self,
        *,
        payload: dict[str, Any] | None = None,
        rehash: bool = True,
        **envelope_changes: object,
    ) -> RuntimeEvent:
        wire = deepcopy(self.example)
        if payload is not None:
            wire["payload"] = deepcopy(payload)
        wire.update(envelope_changes)
        if rehash:
            wire["payload"]["inference_sha256"] = learner_inference_sha256(wire["payload"])
        content_ref = wire["content_ref"]
        return RuntimeEvent(
            event_id=wire["event_id"],
            event_type=wire["event_type"],
            event_version=wire["event_version"],
            schema_version=wire["schema_version"],
            stream_id=wire["stream_id"],
            sequence=wire["sequence"],
            occurred_at=datetime.fromisoformat(wire["occurred_at"].replace("Z", "+00:00")),
            producer=wire["producer"],
            trace_id=wire["trace_id"],
            command_id=wire["command_id"],
            correlation_id=wire["correlation_id"],
            causation_id=wire["causation_id"],
            content_ref=ContentRef(
                content_ref["unit_id"],
                content_ref["version"],
                content_ref["content_hash"],
            ),
            payload=wire["payload"],
        )

    def payload(self) -> dict[str, Any]:
        return deepcopy(self.example["payload"])

    def test_positive_example_is_schema_valid_hash_bound_and_runtime_valid(self) -> None:
        self.schema_validator.validate(
            "schemas/learner/learner-inference-recorded-event.schema.json",
            self.example,
        )
        self.assertEqual(
            self.example["payload"]["inference_sha256"],
            learner_inference_sha256(self.example["payload"]),
        )
        event = self.runtime_event()
        self.assertEqual(event.event_type, RuntimeEventType.LEARNER_INFERENCE_RECORDED)
        self.assertEqual(event.schema_version, "2.0.0")

    def test_hash_is_order_independent_and_decimal_integer_stable(self) -> None:
        payload = self.payload()
        source = {key: value for key, value in reversed(tuple(payload.items()))}
        self.assertEqual(
            learner_inference_sha256(payload),
            learner_inference_sha256(source),
        )

        decimal_short = json.loads(
            json.dumps(payload).replace('"score_delta": 0.2', '"score_delta": 0.200000')
        )
        self.assertEqual(
            learner_inference_sha256(payload),
            learner_inference_sha256(decimal_short),
        )

        integer_number = self.payload()
        integer_number["score_delta"] = 0
        integer_number["confidence"] = 1
        floating_number = deepcopy(integer_number)
        floating_number["score_delta"] = 0.0
        floating_number["confidence"] = 1.0
        self.assertEqual(
            learner_inference_sha256(integer_number),
            learner_inference_sha256(floating_number),
        )

    def test_rejects_envelope_identity_and_version_substitution(self) -> None:
        invalid_cases = [
            ("schema_version", {"schema_version": "1.0.0"}, "schema_version 2.0.0"),
            ("stream", {"stream_id": "learner:student_other_0001"}, "stream_id"),
            ("command", {"command_id": "cmd_other_inference_00000001"}, "command_id"),
            (
                "causation",
                {"causation_id": "evt_other_feedback_00000001"},
                "causation_id",
            ),
        ]
        for label, changes, message in invalid_cases:
            with self.subTest(case=label), self.assertRaisesRegex(ValueError, message):
                self.runtime_event(**changes)

    def test_rejects_actor_role_bounds_precision_and_payload_substitution(self) -> None:
        actor_mismatch = self.payload()
        actor_mismatch["actor"]["actor_id"] = "student_other_0001"
        role_mismatch = self.payload()
        role_mismatch["role"] = "world_agent"
        score_out_of_bounds = self.payload()
        score_out_of_bounds["score_delta"] = 0.300001
        confidence_out_of_bounds = self.payload()
        confidence_out_of_bounds["confidence"] = 1.000001

        invalid_cases = [
            ("actor", actor_mismatch, "actor.actor_id must equal learner_id"),
            ("role", role_mismatch, "role is not supported"),
            ("score", score_out_of_bounds, "outside its contract bounds"),
            ("confidence", confidence_out_of_bounds, "outside its contract bounds"),
        ]
        for label, payload, message in invalid_cases:
            with self.subTest(case=label), self.assertRaisesRegex(ValueError, message):
                self.runtime_event(payload=payload)

        excessive_precision = self.payload()
        excessive_precision["confidence"] = 0.1234567
        with self.assertRaisesRegex(ValueError, "at most six decimal places"):
            learner_inference_sha256(excessive_precision)

        extra_field = self.payload()
        extra_field["untrusted_projection"] = True
        with self.assertRaisesRegex(ValueError, "extra keys"):
            learner_inference_sha256(extra_field)

    def test_rejects_unhashed_unsorted_or_empty_evidence_and_hash_tampering(self) -> None:
        missing_hash = self.payload()
        del missing_hash["evidence_refs"][0]["sha256"]
        unsorted = self.payload()
        unsorted["evidence_refs"].reverse()
        empty = self.payload()
        empty["evidence_refs"] = []

        invalid_cases = [
            ("missing hash", missing_hash, "sha256 is required"),
            ("unsorted", unsorted, "strictly sorted"),
            ("empty", empty, "at least one item"),
        ]
        for label, payload, message in invalid_cases:
            with self.subTest(case=label), self.assertRaisesRegex(ValueError, message):
                self.runtime_event(payload=payload)

        tampered = self.payload()
        tampered["reason"] = "A substituted reason that was not covered by the persisted hash."
        with self.assertRaisesRegex(ValueError, "does not match its canonical payload"):
            self.runtime_event(payload=tampered, rehash=False)


if __name__ == "__main__":
    unittest.main()
