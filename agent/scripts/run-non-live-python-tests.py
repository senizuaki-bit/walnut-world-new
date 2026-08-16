"""Run the complete Python gate while proving live Provider tests were not run."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Iterable
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(REPOSITORY_ROOT / "python"),
    str(REPOSITORY_ROOT / "tests"),
    str(REPOSITORY_ROOT),
]

LIVE_TEST_IDS = frozenset(
    {
        "test_agent_backend_public_role_live_e2e."
        "PublicStudentChainRoleLiveE2E."
        "test_public_v1_v2_chain_real_provider_replay_and_restart",
        "test_agent_backend_role_live_e2e."
        "AgentBackendRoleLiveE2E."
        "test_real_three_failures_bug_then_success_book_replay_restart",
    }
)
EXPECTED_DISCOVERED_TESTS = 601
EXPECTED_NON_LIVE_TESTS = 599


def _flatten(suite: unittest.TestSuite) -> Iterable[unittest.case.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        elif isinstance(item, unittest.case.TestCase):
            yield item
        else:
            raise TypeError(f"unsupported unittest item: {type(item).__name__}")


def _validate_discovery_ids(
    test_ids: Iterable[str],
    *,
    import_failure_ids: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    discovered = tuple(test_ids)
    import_failures = tuple(import_failure_ids)
    included = tuple(test_id for test_id in discovered if test_id not in LIVE_TEST_IDS)
    excluded = tuple(test_id for test_id in discovered if test_id in LIVE_TEST_IDS)
    failures: list[str] = []

    if import_failures:
        failures.append(f"import failures={sorted(import_failures)!r}")
    if len(discovered) != len(set(discovered)):
        failures.append("duplicate test ids")
    if len(discovered) != EXPECTED_DISCOVERED_TESTS:
        failures.append(
            f"discovered count expected={EXPECTED_DISCOVERED_TESTS} actual={len(discovered)}"
        )
    if len(included) != EXPECTED_NON_LIVE_TESTS:
        failures.append(f"non-live count expected={EXPECTED_NON_LIVE_TESTS} actual={len(included)}")
    if frozenset(excluded) != LIVE_TEST_IDS or len(excluded) != len(LIVE_TEST_IDS):
        failures.append(
            f"live exclusions expected={sorted(LIVE_TEST_IDS)!r} actual={sorted(excluded)!r}"
        )
    if failures:
        raise RuntimeError("; ".join(failures))
    return included, tuple(sorted(excluded))


def _is_import_failure(test: unittest.case.TestCase) -> bool:
    test_type = type(test)
    return test_type.__module__ == "unittest.loader" and test_type.__name__ == "_FailedTest"


def main() -> int:
    discovered = unittest.defaultTestLoader.discover(
        str(REPOSITORY_ROOT / "tests"),
        pattern="test_*.py",
        top_level_dir=str(REPOSITORY_ROOT / "tests"),
    )
    collected = tuple(_flatten(discovered))
    try:
        included_ids, excluded = _validate_discovery_ids(
            (test.id() for test in collected),
            import_failure_ids=(test.id() for test in collected if _is_import_failure(test)),
        )
    except RuntimeError as error:
        print(
            f"NON_LIVE_DISCOVERY_FAILED {error}",
            file=sys.stderr,
        )
        return 2
    included_by_id = set(included_ids)
    included = unittest.TestSuite(test for test in collected if test.id() in included_by_id)
    for test_id in sorted(excluded):
        print(f"EXCLUDED_NOT_RUN {test_id}", flush=True)

    result = unittest.TextTestRunner(verbosity=2).run(included)
    if result.testsRun != EXPECTED_NON_LIVE_TESTS:
        print(
            "NON_LIVE_RUN_COUNT_FAILED "
            f"expected={EXPECTED_NON_LIVE_TESTS} actual={result.testsRun}",
            file=sys.stderr,
        )
        return 4
    if result.skipped:
        for test, reason in result.skipped:
            print(f"UNEXPECTED_SKIP {test.id()} reason={reason}", file=sys.stderr)
        return 3
    if not result.wasSuccessful():
        return 1
    print(
        "AGENT_NON_LIVE_PYTHON_TESTS_OK "
        f"run={result.testsRun} excluded_not_run={len(excluded)} skipped=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
