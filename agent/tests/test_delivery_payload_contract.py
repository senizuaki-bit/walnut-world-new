from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from yaya_agent_contracts import (
    ActorRef,
    ContentRef,
    DeliveryPayload,
    DeliveryReceipt,
    FeishuReportDraftBody,
    OperationContext,
    OutboxMessage,
    OutboxStatus,
)

AGENT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = AGENT_ROOT / "contracts" / "schemas" / "delivery" / "delivery-payload.schema.json"
RECEIPT_SCHEMA_PATH = (
    AGENT_ROOT / "contracts" / "schemas" / "delivery" / "delivery-receipt.schema.json"
)


def accepts_python(value: dict[str, Any]) -> bool:
    candidate = dict(value)
    body = candidate.get("body")
    try:
        if isinstance(body, dict):
            candidate["body"] = FeishuReportDraftBody(**body)
        DeliveryPayload(**candidate)
    except (TypeError, ValueError):
        return False
    return True


def accepts_python_receipt(value: dict[str, Any]) -> bool:
    candidate = dict(value)
    sent_at = candidate.get("sent_at")
    try:
        if isinstance(sent_at, str):
            candidate["sent_at"] = datetime.fromisoformat(sent_at)
        DeliveryReceipt(**candidate)
    except (TypeError, ValueError):
        return False
    return True


class DeliveryPayloadContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator_type = validator_for(cls.schema)
        validator_type.check_schema(cls.schema)
        cls.validator = validator_type(cls.schema, format_checker=FormatChecker())
        cls.receipt_schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
        receipt_validator_type = validator_for(cls.receipt_schema)
        receipt_validator_type.check_schema(cls.receipt_schema)
        cls.receipt_validator = receipt_validator_type(
            cls.receipt_schema,
            format_checker=FormatChecker(),
        )

    def assert_differential_acceptance(self, value: dict[str, Any], expected: bool) -> None:
        schema_accepts = not list(self.validator.iter_errors(value))
        self.assertEqual(expected, schema_accepts, "JSON Schema acceptance drifted")
        self.assertEqual(expected, accepts_python(value), "Python DTO acceptance drifted")

    def test_schema_and_python_accept_exact_report_draft_payload(self) -> None:
        self.assert_differential_acceptance(
            {
                "delivery_id": "delivery_0001",
                "operation": "FEISHU_REPORT_DRAFT",
                "deduplication_key": "delivery:req_python_0001",
                "attempt": 1,
                "body": {"report_id": "report_0001"},
            },
            True,
        )

    def test_schema_and_python_reject_operation_and_shape_mutations(self) -> None:
        base: dict[str, Any] = {
            "delivery_id": "delivery_0001",
            "operation": "FEISHU_REPORT_DRAFT",
            "deduplication_key": "delivery:req_python_0001",
            "attempt": 1,
            "body": {"report_id": "report_0001"},
        }
        mutations = {
            "unknown operation": {**base, "operation": "EMAIL_REPORT_DRAFT"},
            "missing report id": {**base, "body": {}},
            "extra body field": {
                **base,
                "body": {"report_id": "report_0001", "ignored_typo": True},
            },
            "invalid report id": {**base, "body": {"report_id": "short"}},
            "extra payload field": {**base, "ignored_typo": True},
            "invalid attempt": {**base, "attempt": 0},
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                self.assert_differential_acceptance(mutation, False)

    def test_python_boundary_rejects_untyped_body_mapping(self) -> None:
        with self.assertRaisesRegex(TypeError, "FeishuReportDraftBody"):
            DeliveryPayload(
                delivery_id="delivery_0001",
                operation="FEISHU_REPORT_DRAFT",
                deduplication_key="delivery:req_python_0001",
                attempt=1,
                body={"report_id": "report_0001"},  # type: ignore[arg-type]
            )

    def test_schema_and_python_receipt_require_request_identity(self) -> None:
        valid: dict[str, Any] = {
            "delivery_id": "delivery_0001",
            "operation": "FEISHU_REPORT_DRAFT",
            "deduplication_key": "delivery:req_python_0001",
            "report_id": "report_0001",
            "remote_object_id": "doccn_remote_0001",
            "sent_at": "2026-08-08T00:00:00+08:00",
            "attempt": 1,
            "status": "SENT",
        }
        self.assertFalse(list(self.receipt_validator.iter_errors(valid)))
        self.assertTrue(accepts_python_receipt(valid))
        for field in ("operation", "deduplication_key", "report_id"):
            mutation = dict(valid)
            mutation.pop(field)
            with self.subTest(missing=field):
                self.assertTrue(list(self.receipt_validator.iter_errors(mutation)))
                self.assertFalse(accepts_python_receipt(mutation))

    def test_outbox_binds_payload_and_receipt_to_the_same_request(self) -> None:
        created_at = datetime.now(UTC)
        context = OperationContext(
            request_id="req_delivery_0001",
            correlation_id="corr_delivery_0001",
            trace_id="trace_delivery_0001",
            requested_at=created_at,
            actor=ActorRef("tenant_yaya", "service_delivery", "service"),
            content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
            command_id="cmd_delivery_0001",
            causation_id=None,
        )
        payload = DeliveryPayload(
            delivery_id="delivery_0001",
            operation="FEISHU_REPORT_DRAFT",
            deduplication_key="delivery:req_python_0001",
            attempt=1,
            body=FeishuReportDraftBody(report_id="report_0001"),
        )
        base: dict[str, Any] = {
            "message_id": "delivery_0001",
            "destination": "FEISHU_REPORT_DRAFT",
            "idempotency_key": "delivery:req_python_0001",
            "payload": payload,
            "created_at": created_at,
            "operation_context": context,
        }
        pending = OutboxMessage(**base)
        self.assertEqual(OutboxStatus.PENDING, pending.status)

        receipt = DeliveryReceipt(
            delivery_id="delivery_0001",
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
        self.assertEqual(receipt, sent.delivery_receipt)

        for untyped_payload in ({}, {"report_id": "report_0001"}):
            with self.subTest(untyped_payload=untyped_payload):
                with self.assertRaisesRegex(TypeError, "DeliveryPayload"):
                    OutboxMessage(**{**base, "payload": untyped_payload})

        with self.assertRaisesRegex(ValueError, "destination"):
            OutboxMessage(**{**base, "destination": "EMAIL_REPORT_DRAFT"})

        payload_mutations = (
            replace(payload, delivery_id="delivery_0002"),
            replace(payload, deduplication_key="delivery:req_python_0002"),
        )
        for mutation in payload_mutations:
            with self.subTest(payload=mutation):
                with self.assertRaisesRegex(ValueError, "payload identity"):
                    OutboxMessage(**{**base, "payload": mutation})

        receipt_mutations = (
            replace(receipt, delivery_id="delivery_0002"),
            replace(receipt, deduplication_key="delivery:req_python_0002"),
            replace(receipt, report_id="report_0002"),
        )
        for mutation in receipt_mutations:
            with self.subTest(receipt=mutation):
                with self.assertRaisesRegex(ValueError, "receipt identity"):
                    OutboxMessage(
                        **base,
                        status=OutboxStatus.SENT,
                        attempt=1,
                        delivery_receipt=mutation,
                    )


if __name__ == "__main__":
    unittest.main()
