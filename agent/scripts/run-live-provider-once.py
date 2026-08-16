"""Run exactly one explicitly budgeted real-Provider E2E with no retry loop."""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "tests"), str(ROOT)]

LIVE_TEST_IDS = (
    "test_agent_backend_role_live_e2e.AgentBackendRoleLiveE2E."
    "test_real_three_failures_bug_then_success_book_replay_restart",
    "test_agent_backend_public_role_live_e2e.PublicStudentChainRoleLiveE2E."
    "test_public_v1_v2_chain_real_provider_replay_and_restart",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one real Provider E2E once; this wrapper never retries.",
    )
    parser.add_argument("--test-id", choices=LIVE_TEST_IDS, required=True)
    parser.add_argument("--generation-budget", type=int, required=True)
    arguments = parser.parse_args()
    if not 1 <= arguments.generation_budget <= 64:
        parser.error("--generation-budget must be between 1 and 64")
    if os.environ.get("YAYA_LIVE_ONESHOT_ACTIVE"):
        parser.error("nested or repeated one-shot invocation is forbidden")

    os.environ["YAYA_LIVE_ONESHOT_ACTIVE"] = "1"
    os.environ["YAYA_LIVE_GENERATION_BUDGET"] = str(arguments.generation_budget)
    suite = unittest.defaultTestLoader.loadTestsFromName(arguments.test_id)
    if suite.countTestCases() != 1:
        print(
            f"LIVE_TEST_DISCOVERY_FAILED id={arguments.test_id} count={suite.countTestCases()}",
            file=sys.stderr,
        )
        return 2
    print(
        "LIVE_PROVIDER_ONE_SHOT_START "
        f"test_id={arguments.test_id} generation_budget={arguments.generation_budget}",
        flush=True,
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("LIVE_PROVIDER_ONE_SHOT_SKIP_FORBIDDEN", file=sys.stderr)
        return 3
    if not result.wasSuccessful():
        return 1
    print("LIVE_PROVIDER_ONE_SHOT_OK attempts=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
