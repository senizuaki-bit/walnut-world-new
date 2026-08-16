from __future__ import annotations

import ast
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_non_live_runner() -> ModuleType:
    path = ROOT / "scripts" / "run-non-live-python-tests.py"
    spec = importlib.util.spec_from_file_location("yaya_non_live_test_runner", path)
    if spec is None or spec.loader is None:
        raise AssertionError("non-live runner import specification is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NonLiveRunnerContractTests(unittest.TestCase):
    def test_runner_excludes_exact_live_ids_and_rejects_any_skip(self) -> None:
        path = ROOT / "scripts" / "run-non-live-python-tests.py"
        source = path.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn(
            "test_real_three_failures_bug_then_success_book_replay_restart",
            source,
        )
        self.assertIn(
            "test_public_v1_v2_chain_real_provider_replay_and_restart",
            source,
        )
        self.assertIn("EXCLUDED_NOT_RUN", source)
        self.assertIn("UNEXPECTED_SKIP", source)
        self.assertNotIn("skipTest(", source)

        runner = _load_non_live_runner()
        self.assertEqual(runner.EXPECTED_DISCOVERED_TESTS, 601)
        self.assertEqual(runner.EXPECTED_NON_LIVE_TESTS, 599)
        non_live_ids = tuple(
            f"synthetic_non_live.Case.test_{index:04d}"
            for index in range(runner.EXPECTED_NON_LIVE_TESTS)
        )
        discovery_ids = (*non_live_ids, *sorted(runner.LIVE_TEST_IDS))
        included, excluded = runner._validate_discovery_ids(
            discovery_ids,
            import_failure_ids=(),
        )
        self.assertEqual(included, non_live_ids)
        self.assertEqual(excluded, tuple(sorted(runner.LIVE_TEST_IDS)))
        failed_import_suite = unittest.TestLoader().loadTestsFromName(
            "synthetic_missing_non_live_module.Case.test_import_failure"
        )
        failed_import = next(iter(runner._flatten(failed_import_suite)))
        self.assertTrue(runner._is_import_failure(failed_import))

        cases = (
            (
                "import failure",
                discovery_ids,
                (non_live_ids[0],),
                "import failures",
            ),
            (
                "new test",
                (*discovery_ids, "synthetic_non_live.Case.test_added"),
                (),
                "discovered count",
            ),
            (
                "lost test",
                (*non_live_ids[1:], *sorted(runner.LIVE_TEST_IDS)),
                (),
                "discovered count",
            ),
        )
        for label, actual_ids, import_failures, expected_message in cases:
            with self.subTest(case=label):
                with self.assertRaisesRegex(RuntimeError, expected_message):
                    runner._validate_discovery_ids(
                        actual_ids,
                        import_failure_ids=import_failures,
                    )

    def test_verify_all_uses_owned_non_live_runner(self) -> None:
        source = (ROOT / "scripts" / "verify-all.ps1").read_text(encoding="utf-8")
        self.assertIn('"scripts/run-non-live-python-tests.py"', source)
        self.assertNotIn('"unittest",\n        "discover"', source)

    def test_package_python_entrypoint_uses_owned_non_live_runner(self) -> None:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(
            package["scripts"]["test:python"],
            "python scripts/run-non-live-python-tests.py",
        )

    def test_live_wrapper_requires_one_test_and_a_generation_budget(self) -> None:
        source = (ROOT / "scripts" / "run-live-provider-once.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("--test-id", source)
        self.assertIn("--generation-budget", source)
        self.assertIn("suite.countTestCases() != 1", source)
        self.assertNotIn("while ", source)
        self.assertNotIn("for attempt", source)

    def test_live_budget_is_enforced_before_each_provider_dispatch(self) -> None:
        live = (ROOT / "tests" / "test_agent_backend_live_e2e.py").read_text(encoding="utf-8")
        role = (ROOT / "tests" / "test_agent_backend_role_live_e2e.py").read_text(encoding="utf-8")
        public = (ROOT / "tests" / "test_agent_backend_public_role_live_e2e.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("class GenerationBudgetTransport", live)
        self.assertIn("_remaining <= 0", live)
        self.assertIn("_generation_budget_guard", role)
        self.assertIn("_generation_budget_guard", public)


if __name__ == "__main__":
    unittest.main()
