from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_contracts import (  # noqa: E402
    ContractInvariantViolation,
    validate_class_insights_privacy,
    validate_client_event_batch,
    validate_world_event_page,
)

AGENT_ROOT = Path(__file__).resolve().parents[1]


def example(name: str) -> dict:
    path = AGENT_ROOT / "contracts" / "examples" / name
    return json.loads(path.read_text(encoding="utf-8"))["value"]


class PythonInvariantTests(unittest.TestCase):
    def test_world_event_page_is_gap_free_unique_and_world_scoped(self) -> None:
        page = example("game-world-event-page.json")
        validate_world_event_page(page, expected_after_sequence=731)

        for mutation in ("gap", "duplicate", "wrong_stream", "wrong_cursor"):
            invalid = deepcopy(page)
            if mutation == "gap":
                invalid["events"][1]["sequence"] = 734
                invalid["to_sequence"] = 734
                invalid["next_after_sequence"] = 734
            elif mutation == "duplicate":
                invalid["events"][1]["event_id"] = invalid["events"][0]["event_id"]
            elif mutation == "wrong_stream":
                invalid["events"][1]["stream_id"] = "world:another_world"
            else:
                invalid["next_after_sequence"] = 734
            with self.subTest(mutation=mutation):
                with self.assertRaises(ContractInvariantViolation):
                    validate_world_event_page(invalid, expected_after_sequence=731)

    def test_empty_world_page_cannot_advance_cursor(self) -> None:
        page = {
            "world_id": "world_demo_001",
            "from_sequence": 733,
            "to_sequence": 733,
            "next_after_sequence": 733,
            "events": [],
        }
        validate_world_event_page(page, expected_after_sequence=733)
        page["next_after_sequence"] = 734
        with self.assertRaises(ContractInvariantViolation):
            validate_world_event_page(page, expected_after_sequence=733)

    def test_client_event_batch_rejects_boundary_gap_and_duplicate(self) -> None:
        batch = example("game-client-event-batch-request.json")
        validate_client_event_batch(batch)
        mutations = []
        wrong_last = deepcopy(batch)
        wrong_last["last_sequence"] += 1
        mutations.append(wrong_last)
        gap = deepcopy(batch)
        gap["events"][-1]["sequence"] += 1
        gap["last_sequence"] += 1
        mutations.append(gap)
        duplicate = deepcopy(batch)
        if len(duplicate["events"]) == 1:
            duplicate["events"].append(deepcopy(duplicate["events"][0]))
            duplicate["events"][1]["sequence"] += 1
            duplicate["last_sequence"] += 1
        else:
            duplicate["events"][1]["event_id"] = duplicate["events"][0]["event_id"]
        mutations.append(duplicate)
        for invalid in mutations:
            with self.assertRaises(ContractInvariantViolation):
                validate_client_event_batch(invalid)

    def test_effective_privacy_threshold_cannot_be_bypassed(self) -> None:
        result = example("feishu-class-insights-response.json")
        validate_class_insights_privacy(result)

        threshold_downgrade = deepcopy(result)
        threshold_downgrade["privacy"]["minimum_cohort_size"] = 10
        threshold_downgrade["privacy"]["effective_minimum_cohort_size"] = 5

        unsuppressed_small_cell = deepcopy(result)
        unsuppressed_small_cell["privacy"]["effective_minimum_cohort_size"] = 10
        unsuppressed_small_cell["insights"][0]["learner_count"] = 9

        small_cohort = deepcopy(result)
        small_cohort["cohort_size"] = 4

        leaked_suppressed_value = deepcopy(result)
        leaked_suppressed_value["insights"][1]["learner_count"] = 1

        for invalid in (
            threshold_downgrade,
            unsuppressed_small_cell,
            small_cohort,
            leaked_suppressed_value,
        ):
            with self.assertRaises(ContractInvariantViolation):
                validate_class_insights_privacy(invalid)


if __name__ == "__main__":
    unittest.main()
