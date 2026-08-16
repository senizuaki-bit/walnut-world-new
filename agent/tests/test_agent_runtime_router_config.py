from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import make_event  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    AgentConfigurationError,
    PackagedRoleConfigProvider,
    RoleRouter,
    calculate_hint_level,
    parse_role_config,
)


class AgentRuntimeRouterAndRoleConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_exhaustively_routes_every_supported_event(self) -> None:
        router = RoleRouter(bug_failure_threshold=3)
        expected = {
            "task_started": "world_agent",
            "compile_succeeded": None,
            "compile_failed": "teaching_agent",
            "run_skill_requested": "xiaohutao",
            "run_succeeded": None,
            "run_failed": "teaching_agent",
            "task_completed": "book_agent",
            "hint_requested": "teaching_agent",
            "skill_patch_confirmed": None,
        }

        routes = {event_type: router.route(make_event(event_type)).role for event_type in expected}

        self.assertEqual(routes, expected)
        self.assertEqual(
            router.route(make_event("run_failed", failure_count=3)).role,
            "bug_agent",
        )
        self.assertEqual(
            router.route(make_event("hint_requested", failure_count=3)).role,
            "bug_agent",
        )

    async def test_no_action_events_are_explicit_and_never_claim_a_role(self) -> None:
        router = RoleRouter()
        no_action_events = {
            "compile_succeeded",
            "run_succeeded",
            "skill_patch_confirmed",
        }

        for event_type in no_action_events:
            with self.subTest(event_type=event_type):
                route = router.route(make_event(event_type))
                self.assertFalse(route.should_run)
                self.assertIsNone(route.role)
                self.assertTrue(route.reason)

    async def test_hint_policy_has_bounded_deterministic_boundaries(self) -> None:
        self.assertEqual(
            [calculate_hint_level(count, requested_hint=False) for count in range(7)],
            [0, 0, 1, 2, 2, 3, 3],
        )
        self.assertEqual(calculate_hint_level(20, requested_hint=True), 4)
        self.assertEqual(calculate_hint_level(20, requested_hint=True, maximum=2), 2)

    async def test_all_five_packaged_role_configs_load_strictly(self) -> None:
        provider = PackagedRoleConfigProvider.load()
        roles = (
            "world_agent",
            "xiaohutao",
            "teaching_agent",
            "bug_agent",
            "book_agent",
        )

        configs = [provider.get(role) for role in roles]

        self.assertEqual([config.id for config in configs], list(roles))
        self.assertTrue(all(config.allowed_events for config in configs))
        self.assertTrue(all(config.response_schema == "AgentDecisionV1" for config in configs))
        self.assertEqual(provider.get("xiaohutao").allowed_events, ("run_skill_requested",))
        self.assertIn("invoke_skill", provider.get("xiaohutao").allowed_tools)

    async def test_role_config_rejects_duplicate_keys_before_validation(self) -> None:
        raw = json.dumps(_valid_world_role_payload())
        duplicate = raw.replace(
            '"id": "world_agent"',
            '"id": "world_agent", "id": "world_agent"',
            1,
        )

        with self.assertRaises(AgentConfigurationError) as raised:
            parse_role_config(duplicate, expected_role="world_agent")

        self.assertEqual(raised.exception.code, "ROLE_CONFIG_DUPLICATE_KEY")
        self.assertEqual(raised.exception.details["key"], "id")

    async def test_role_config_rejects_unknown_or_missing_fields(self) -> None:
        payload = _valid_world_role_payload()
        payload["silent_default"] = True

        with self.assertRaises(AgentConfigurationError) as raised:
            parse_role_config(json.dumps(payload), expected_role="world_agent")

        self.assertEqual(raised.exception.code, "ROLE_CONFIG_KEYS_MISMATCH")
        self.assertEqual(raised.exception.details["extra"], ["silent_default"])


def _valid_world_role_payload() -> dict[str, object]:
    return {
        "id": "world_agent",
        "display_name": "World",
        "purpose": "Introduce one objective game task.",
        "allowed_events": ["task_started"],
        "allowed_tools": [],
        "response_schema": "AgentDecisionV1",
        "temperature": 0.0,
        "max_output_tokens": 256,
        "timeout_ms": 1000,
        "prompt": "Use only facts in the bounded turn context.",
        "limits": {
            "max_tool_calls": 0,
            "max_message_chars": 500,
            "allow_skill_patch": False,
            "require_confirmation_for_patch": False,
        },
    }


if __name__ == "__main__":
    unittest.main()
