from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_contracts import (  # noqa: E402
    AgentTurnFeedback,
    ContentRef,
    EvidenceRef,
    EvidenceType,
    RuntimeEvent,
    RuntimeEventType,
)


class AgentFeedbackContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(UTC)
        self.evidence = EvidenceRef(
            evidence_id="evidence_feedback_00000001",
            evidence_type=EvidenceType.WORLD_COMMIT,
            created_at=self.now,
            sha256="e" * 64,
        )

    def feedback(self, **changes: object) -> AgentTurnFeedback:
        values: dict[str, object] = {
            "session_id": "session_demo_001",
            "turn_id": "turn_demo_000001",
            "command_id": "cmd_feedback_00000001",
            "run_id": "run_water_0001",
            "message_key": "agent.turn.completed",
            "message": "The requested turn is complete.",
            "source": "provider",
            "degraded": False,
            "fallback_reason": None,
            "evidence_refs": (self.evidence,),
            "completed_at": self.now,
        }
        values.update(changes)
        return AgentTurnFeedback(**values)  # type: ignore[arg-type]

    def event(
        self, payload: dict[str, object], *, command_id: str = "cmd_feedback_00000001"
    ) -> RuntimeEvent:
        return RuntimeEvent(
            event_id="evt_feedback_00000001",
            event_type="agent.turn.feedback_ready",
            event_version=1,
            stream_id="agent-session:session_demo_001",
            sequence=18,
            occurred_at=self.now,
            producer="agent_hub",
            trace_id="trace_feedback_00000001",
            command_id=command_id,
            correlation_id="corr_feedback_00000001",
            causation_id="evt_world_00000001",
            content_ref=ContentRef("YAYA_FARM_001", "1.4.0", "a" * 64),
            payload=payload,
        )

    def wire_payload(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": "session_demo_001",
            "turn_id": "turn_demo_000001",
            "command_id": "cmd_feedback_00000001",
            "run_id": "run_water_0001",
            "message_key": "agent.turn.completed",
            "message": "The requested turn is complete.",
            "source": "provider",
            "degraded": False,
            "fallback_reason": None,
            "evidence_refs": [],
            "completed_at": "2026-08-07T12:00:04Z",
        }
        payload.update(changes)
        return payload

    def test_python_dto_is_a_strict_discriminated_union_at_runtime(self) -> None:
        self.assertEqual(self.feedback().source, "provider")
        fallback = self.feedback(
            run_id=None,
            source="provider_fallback",
            degraded=True,
            fallback_reason="MODEL_OUTPUT_INVALID",
        )
        self.assertTrue(fallback.degraded)
        with self.assertRaisesRegex(ValueError, "provider_fallback"):
            self.feedback(degraded=True, fallback_reason="MODEL_OUTPUT_INVALID")
        with self.assertRaisesRegex(ValueError, "provider source"):
            self.feedback(source="provider_fallback")
        with self.assertRaisesRegex(ValueError, "unique evidence_id"):
            self.feedback(evidence_refs=(self.evidence, self.evidence))

    def test_runtime_event_enforces_payload_shape_and_command_linkage(self) -> None:
        event = self.event(self.wire_payload())
        self.assertEqual(event.event_type, RuntimeEventType.AGENT_TURN_FEEDBACK_READY)
        with self.assertRaisesRegex(ValueError, "must equal envelope command_id"):
            self.event(self.wire_payload(command_id="cmd_other_00000001"))
        with self.assertRaisesRegex(ValueError, "provider_fallback"):
            self.event(self.wire_payload(degraded=True, fallback_reason="MODEL_OUTPUT_INVALID"))
        with self.assertRaisesRegex(ValueError, "extra keys"):
            self.event(self.wire_payload(silent_extra_field=True))


if __name__ == "__main__":
    unittest.main()
