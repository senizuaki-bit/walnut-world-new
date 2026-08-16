from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.product_application import (  # noqa: E402
    ProductApplicationError,
    ProductInteractionReadApplication,
)
from yaya_agent_backend.product_repositories import (  # noqa: E402
    ProductInteractionPageSnapshot,
    ProductInteractionSnapshot,
    ProductReadCursorError,
    ProductReadDependencyError,
    ProductReadInvariantError,
    ProductReadNotFoundError,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    canonical_json_sha256,
)

_SESSION_ID = "session_agent_001"
_INTERACTION_ID = "interaction_water_001"


def _example(name: str) -> dict[str, object]:
    document = json.loads(
        (CONTRACTS_ROOT / "examples" / f"{name}.json").read_text(encoding="utf-8")
    )
    value = document["value"]
    if not isinstance(value, dict):
        raise AssertionError(f"frozen example {name} is not an object")
    return cast(dict[str, object], value)


def _actor() -> ActorRef:
    return ActorRef(
        tenant_id="tenant_yaya",
        actor_id="student_0001",
        actor_type=ActorType.STUDENT,
        roles=("game:player",),
    )


def _valid_interaction() -> dict[str, object]:
    page = _example("product-agent-interaction-page")
    interactions = cast(list[dict[str, object]], page["interactions"])
    interaction = copy.deepcopy(interactions[0])
    context = cast(dict[str, object], interaction["request_context"])
    feedback_event = cast(dict[str, object], interaction["feedback_event"])
    # The production projection retains the Agent-turn origin.  Align the old
    # illustrative Product attempt identifiers with that canonical event before
    # using the frozen example as executable application data.
    context["trace_id"] = feedback_event["trace_id"]
    context["correlation_id"] = feedback_event["correlation_id"]
    return interaction


def _rehash_interaction(interaction: dict[str, object]) -> None:
    feedback = cast(dict[str, object], interaction["feedback"])
    feedback_event = cast(dict[str, object], interaction["feedback_event"])
    source = cast(dict[str, object], interaction["projection_source"])
    feedback_sha256 = canonical_json_sha256(feedback)
    feedback_event["feedback_sha256"] = feedback_sha256
    source["feedback_sha256"] = feedback_sha256
    source["source_sha256"] = canonical_json_sha256(
        {key: value for key, value in source.items() if key != "source_sha256"}
    )


def _page_snapshot(
    interaction: dict[str, object],
    *,
    high_watermark: int = 1,
    interactions: tuple[ProductInteractionSnapshot, ...] | None = None,
) -> ProductInteractionPageSnapshot:
    selected = (
        (ProductInteractionSnapshot(copy.deepcopy(interaction)),)
        if interactions is None
        else interactions
    )
    return ProductInteractionPageSnapshot(
        request_context=copy.deepcopy(cast(dict[str, object], interaction["request_context"])),
        session_id=cast(str, interaction["session_id"]),
        high_watermark_sequence=high_watermark,
        interactions=selected,
    )


class _FakeRepository:
    def __init__(self, interaction: dict[str, object]) -> None:
        self.page = _page_snapshot(interaction)
        self.interaction = ProductInteractionSnapshot(copy.deepcopy(interaction))
        self.list_error: BaseException | None = None
        self.get_error: BaseException | None = None
        self.list_calls: list[tuple[ActorRef, str, int, int]] = []
        self.get_calls: list[tuple[ActorRef, str, str]] = []

    async def list_interactions(
        self,
        actor: ActorRef,
        session_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> ProductInteractionPageSnapshot:
        self.list_calls.append((actor, session_id, after_sequence, limit))
        if self.list_error is not None:
            raise self.list_error
        return self.page

    async def get_interaction(
        self,
        actor: ActorRef,
        session_id: str,
        interaction_id: str,
    ) -> ProductInteractionSnapshot:
        self.get_calls.append((actor, session_id, interaction_id))
        if self.get_error is not None:
            raise self.get_error
        return self.interaction


class ProductInteractionApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.actor = _actor()
        self.interaction = _valid_interaction()
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self.repository = _FakeRepository(self.interaction)
        self.application = ProductInteractionReadApplication(
            self.repository,
            self.validator,
        )

    async def test_happy_list_and_get_preserve_origin_and_resource_headers(self) -> None:
        list_result = await self.application.list_interactions(
            self.actor,
            _SESSION_ID,
            after_sequence=0,
        )
        self.validator.validate(
            "schemas/product-experience/agent-interaction-page.schema.json",
            list_result.payload,
        )
        self.assertEqual(list_result.headers, {"X-Interaction-High-Watermark": "1"})
        self.assertEqual(list_result.payload["requested_limit"], 50)
        self.assertEqual(list_result.payload["from_sequence"], 1)
        self.assertEqual(list_result.payload["to_sequence"], 1)
        self.assertEqual(list_result.payload["next_after_sequence"], 1)
        self.assertIs(list_result.payload["has_more"], False)
        self.assertEqual(
            list_result.payload["request_context"],
            self.repository.page.request_context,
        )
        self.assertEqual(
            self.repository.list_calls,
            [(self.actor, _SESSION_ID, 0, 50)],
        )

        get_result = await self.application.get_interaction(
            self.actor,
            _SESSION_ID,
            _INTERACTION_ID,
        )
        self.validator.validate(
            "schemas/product-experience/agent-interaction.schema.json",
            get_result.payload,
        )
        self.assertEqual(get_result.payload, self.interaction)
        self.assertEqual(get_result.headers["X-Interaction-Revision"], "1")
        self.assertEqual(
            get_result.headers["ETag"],
            f'"interaction:1:{canonical_json_sha256(get_result.payload)}"',
        )
        self.assertRegex(
            get_result.headers["ETag"],
            re.compile(r'^"interaction:1:[a-f0-9]{64}"$'),
        )

    async def test_empty_page_does_not_advance_and_ahead_cursor_is_400(self) -> None:
        self.repository.page = _page_snapshot(
            self.interaction,
            high_watermark=1,
            interactions=(),
        )
        result = await self.application.list_interactions(
            self.actor,
            _SESSION_ID,
            after_sequence=1,
            limit=1,
        )
        self.assertEqual(result.payload["interactions"], [])
        self.assertIsNone(result.payload["from_sequence"])
        self.assertIsNone(result.payload["to_sequence"])
        self.assertEqual(result.payload["next_after_sequence"], 1)
        self.assertEqual(result.payload["high_watermark_sequence"], 1)
        self.assertIs(result.payload["has_more"], False)

        self.repository.list_error = ProductReadCursorError("cursor is ahead of tip")
        with self.assertRaises(ProductApplicationError) as raised:
            await self.application.list_interactions(
                self.actor,
                _SESSION_ID,
                after_sequence=2,
                limit=1,
            )
        self.assertEqual(
            (raised.exception.code, raised.exception.http_status), ("INVALID_REQUEST", 400)
        )

    async def test_repository_failures_map_to_closed_application_errors(self) -> None:
        list_cases: tuple[tuple[BaseException, str, int], ...] = (
            (ProductReadNotFoundError("missing"), "NOT_FOUND", 404),
            (ProductReadDependencyError("down"), "DEPENDENCY_UNAVAILABLE", 503),
            (ProductReadInvariantError("drift"), "INVARIANT_VIOLATION", 500),
        )
        for error, code, status in list_cases:
            with self.subTest(operation="list", code=code):
                repository = _FakeRepository(self.interaction)
                repository.list_error = error
                application = ProductInteractionReadApplication(repository, self.validator)
                with self.assertRaises(ProductApplicationError) as raised:
                    await application.list_interactions(
                        self.actor,
                        _SESSION_ID,
                        after_sequence=0,
                    )
                self.assertEqual(
                    (raised.exception.code, raised.exception.http_status), (code, status)
                )

        get_cases: tuple[tuple[BaseException, str, int], ...] = (
            (ProductReadNotFoundError("missing"), "NOT_FOUND", 404),
            (ProductReadDependencyError("down"), "DEPENDENCY_UNAVAILABLE", 503),
            (ProductReadInvariantError("drift"), "INVARIANT_VIOLATION", 500),
        )
        for error, code, status in get_cases:
            with self.subTest(operation="get", code=code):
                repository = _FakeRepository(self.interaction)
                repository.get_error = error
                application = ProductInteractionReadApplication(repository, self.validator)
                with self.assertRaises(ProductApplicationError) as raised:
                    await application.get_interaction(
                        self.actor,
                        _SESSION_ID,
                        _INTERACTION_ID,
                    )
                self.assertEqual(
                    (raised.exception.code, raised.exception.http_status), (code, status)
                )

    async def test_invalid_cursor_types_and_bounds_never_reach_repository(self) -> None:
        cases = (
            {"after_sequence": -1, "limit": 50},
            {"after_sequence": True, "limit": 50},
            {"after_sequence": 9_007_199_254_740_992, "limit": 50},
            {"after_sequence": 0, "limit": 0},
            {"after_sequence": 0, "limit": 101},
            {"after_sequence": 0, "limit": False},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ProductApplicationError) as raised:
                    await self.application.list_interactions(
                        self.actor,
                        _SESSION_ID,
                        **values,
                    )
                self.assertEqual(
                    (raised.exception.code, raised.exception.http_status), ("INVALID_REQUEST", 400)
                )
        self.assertEqual(self.repository.list_calls, [])

    async def test_etag_changes_when_a_hash_closed_projection_field_changes(self) -> None:
        first = await self.application.get_interaction(
            self.actor,
            _SESSION_ID,
            _INTERACTION_ID,
        )
        mutated = _valid_interaction()
        feedback = cast(dict[str, object], mutated["feedback"])
        feedback["message"] = cast(str, feedback["message"]) + " Verified."
        _rehash_interaction(mutated)
        repository = _FakeRepository(mutated)
        application = ProductInteractionReadApplication(repository, self.validator)
        second = await application.get_interaction(
            self.actor,
            _SESSION_ID,
            _INTERACTION_ID,
        )
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])
        self.assertEqual(first.headers["X-Interaction-Revision"], "1")
        self.assertEqual(second.headers["X-Interaction-Revision"], "1")

    async def test_get_rejects_schema_and_semantic_projection_drift(self) -> None:
        def schema_drift(value: dict[str, object]) -> None:
            value["untrusted_marker"] = "must-not-escape"

        def actor_drift(value: dict[str, object]) -> None:
            context = cast(dict[str, object], value["request_context"])
            actor = cast(dict[str, object], context["actor"])
            actor["actor_id"] = "student_other_001"

        def content_drift(value: dict[str, object]) -> None:
            context = cast(dict[str, object], value["request_context"])
            content = cast(dict[str, object], context["content_ref"])
            content["content_hash"] = "b" * 64

        def session_drift(value: dict[str, object]) -> None:
            value["session_id"] = "session_other_001"

        def feedback_hash_drift(value: dict[str, object]) -> None:
            feedback = cast(dict[str, object], value["feedback"])
            feedback["message"] = "tampered without rehashing"

        def link_drift(value: dict[str, object]) -> None:
            links = cast(dict[str, object], value["links"])
            links["self"] = (
                "/product-experience/v1/sessions/session_agent_001/"
                "agent-interactions/interaction_other_001"
            )

        mutations: tuple[tuple[str, Callable[[dict[str, object]], None]], ...] = (
            ("schema", schema_drift),
            ("actor", actor_drift),
            ("content", content_drift),
            ("session", session_drift),
            ("hash", feedback_hash_drift),
            ("link", link_drift),
        )
        for name, mutate in mutations:
            with self.subTest(drift=name):
                interaction = _valid_interaction()
                mutate(interaction)
                repository = _FakeRepository(interaction)
                application = ProductInteractionReadApplication(repository, self.validator)
                with self.assertRaises(ProductApplicationError) as raised:
                    await application.get_interaction(
                        self.actor,
                        _SESSION_ID,
                        _INTERACTION_ID,
                    )
                self.assertEqual(
                    (raised.exception.code, raised.exception.http_status),
                    ("INVARIANT_VIOLATION", 500),
                )

    async def test_list_rejects_page_origin_session_and_high_watermark_drift(self) -> None:
        def page_actor_drift(
            snapshot: ProductInteractionPageSnapshot,
        ) -> ProductInteractionPageSnapshot:
            context = copy.deepcopy(cast(dict[str, object], snapshot.request_context))
            actor = cast(dict[str, object], context["actor"])
            actor["actor_id"] = "student_other_001"
            return ProductInteractionPageSnapshot(
                context,
                snapshot.session_id,
                snapshot.high_watermark_sequence,
                snapshot.interactions,
            )

        def page_content_drift(
            snapshot: ProductInteractionPageSnapshot,
        ) -> ProductInteractionPageSnapshot:
            context = copy.deepcopy(cast(dict[str, object], snapshot.request_context))
            content = cast(dict[str, object], context["content_ref"])
            content["content_hash"] = "b" * 64
            return ProductInteractionPageSnapshot(
                context,
                snapshot.session_id,
                snapshot.high_watermark_sequence,
                snapshot.interactions,
            )

        base = _page_snapshot(self.interaction)
        cases = (
            ("actor", page_actor_drift(base)),
            ("content", page_content_drift(base)),
            (
                "session",
                ProductInteractionPageSnapshot(
                    base.request_context,
                    "session_other_001",
                    base.high_watermark_sequence,
                    base.interactions,
                ),
            ),
            (
                "high-watermark",
                ProductInteractionPageSnapshot(
                    base.request_context,
                    base.session_id,
                    0,
                    base.interactions,
                ),
            ),
            (
                "empty-high-watermark",
                ProductInteractionPageSnapshot(
                    base.request_context,
                    base.session_id,
                    2,
                    (),
                ),
            ),
        )
        for name, snapshot in cases:
            with self.subTest(drift=name):
                repository = _FakeRepository(self.interaction)
                repository.page = snapshot
                application = ProductInteractionReadApplication(repository, self.validator)
                with self.assertRaises(ProductApplicationError) as raised:
                    await application.list_interactions(
                        self.actor,
                        _SESSION_ID,
                        after_sequence=1 if name == "empty-high-watermark" else 0,
                    )
                self.assertEqual(
                    (raised.exception.code, raised.exception.http_status),
                    ("INVARIANT_VIOLATION", 500),
                )


if __name__ == "__main__":
    unittest.main()
