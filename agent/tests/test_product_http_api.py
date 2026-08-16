from __future__ import annotations

import copy
import json
import sys
import unittest
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.product_application import (  # noqa: E402
    ProductApplicationError,
    ProductReadResult,
)
from yaya_agent_backend.product_http_api import ProductHttpApi  # noqa: E402
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    canonical_json_sha256,
)

_SESSION_ID = "session_agent_001"
_INTERACTION_ID = "interaction_water_001"
_LIST_TARGET = (
    "/product-experience/v1/sessions/session_agent_001/agent-interactions?after_sequence=0"
)
_GET_TARGET = (
    "/product-experience/v1/sessions/session_agent_001/agent-interactions/interaction_water_001"
)
_JWT_SECRET = "product-http-test-secret-" + "s" * 48
_JWT_ISSUER = "yaya-product-http-test"
_JWT_AUDIENCE = "yaya-product-test"


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
    interaction = copy.deepcopy(cast(list[dict[str, object]], page["interactions"])[0])
    context = cast(dict[str, object], interaction["request_context"])
    feedback_event = cast(dict[str, object], interaction["feedback_event"])
    context["trace_id"] = feedback_event["trace_id"]
    context["correlation_id"] = feedback_event["correlation_id"]
    return interaction


def _valid_page(*, requested_limit: int = 50) -> dict[str, object]:
    interaction = _valid_interaction()
    return {
        "request_context": copy.deepcopy(interaction["request_context"]),
        "session_id": _SESSION_ID,
        "requested_after_sequence": 0,
        "requested_limit": requested_limit,
        "high_watermark_sequence": 1,
        "from_sequence": 1,
        "to_sequence": 1,
        "has_more": False,
        "next_after_sequence": 1,
        "interactions": [interaction],
    }


def _list_result(
    payload: Mapping[str, object],
    *,
    high_watermark_header: str | None = None,
) -> ProductReadResult:
    high = payload.get("high_watermark_sequence")
    return ProductReadResult(
        copy.deepcopy(dict(payload)),
        {
            "X-Interaction-High-Watermark": (
                str(high) if high_watermark_header is None else high_watermark_header
            )
        },
    )


def _get_result(
    payload: Mapping[str, object],
    *,
    etag: str | None = None,
    revision: str | None = None,
) -> ProductReadResult:
    copied = copy.deepcopy(dict(payload))
    interaction_revision = copied.get("interaction_revision")
    return ProductReadResult(
        copied,
        {
            "ETag": (
                f'"interaction:{interaction_revision}:{canonical_json_sha256(copied)}"'
                if etag is None
                else etag
            ),
            "X-Interaction-Revision": (str(interaction_revision) if revision is None else revision),
        },
    )


class _FakeApplication:
    def __init__(self) -> None:
        self.list_result = _list_result(_valid_page())
        self.get_result = _get_result(_valid_interaction())
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
        limit: int = 50,
    ) -> ProductReadResult:
        self.list_calls.append((actor, session_id, after_sequence, limit))
        if self.list_error is not None:
            raise self.list_error
        return self.list_result

    async def get_interaction(
        self,
        actor: ActorRef,
        session_id: str,
        interaction_id: str,
    ) -> ProductReadResult:
        self.get_calls.append((actor, session_id, interaction_id))
        if self.get_error is not None:
            raise self.get_error
        return self.get_result


def _payload(response_body: bytes) -> dict[str, object]:
    value = json.loads(response_body.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("HTTP response is not a JSON object")
    return cast(dict[str, object], value)


class ProductHttpApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.actor = _actor()
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self.authenticator = JwtAuthenticator(
            hmac_secret=_JWT_SECRET,
            issuer=_JWT_ISSUER,
            audience=_JWT_AUDIENCE,
        )
        self.token = self.authenticator.issue_for_test(
            self.actor,
            now=datetime.now(UTC),
        )
        self.application = _FakeApplication()
        self.api = ProductHttpApi(
            application=self.application,
            authenticator=self.authenticator,
            validator=self.validator,
        )

    def _headers(self, suffix: str = "0001") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_product_http_{suffix}",
            "X-Trace-Id": f"trace_product_http_{suffix}",
            "X-Correlation-Id": f"corr_product_http_{suffix}",
        }

    def _assert_attempt_headers(
        self,
        response_headers: Mapping[str, str],
        request_headers: Mapping[str, str],
    ) -> None:
        self.assertEqual(response_headers["X-Request-Id"], request_headers["X-Request-Id"])
        self.assertEqual(response_headers["X-Trace-Id"], request_headers["X-Trace-Id"])
        self.assertEqual(
            response_headers["X-Correlation-Id"],
            request_headers["X-Correlation-Id"],
        )
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertTrue(response_headers["Content-Type"].startswith("application/json"))

    def _assert_error(
        self,
        response_status: int,
        response_headers: Mapping[str, str],
        response_body: bytes,
        request_headers: Mapping[str, str],
        *,
        expected_status: int,
        expected_code: str,
    ) -> dict[str, object]:
        self.assertEqual(response_status, expected_status)
        self._assert_attempt_headers(response_headers, request_headers)
        payload = _payload(response_body)
        self.validator.validate("schemas/common/error-response.schema.json", payload)
        self.validator.validate_reference(
            (
                "https://contracts.yaya.local/product-experience/"
                f"product-error-responses-by-status.schema.json#/$defs/status{expected_status}"
            ),
            payload,
        )
        error = cast(dict[str, object], payload["error"])
        self.assertEqual(error["code"], expected_code)
        self.assertEqual(response_headers["Content-Length"], str(len(response_body)))
        self.assertNotIn("ETag", response_headers)
        self.assertNotIn("X-Interaction-Revision", response_headers)
        self.assertNotIn("X-Interaction-High-Watermark", response_headers)
        return payload

    async def test_happy_list_and_get_enforce_distinct_resource_headers(self) -> None:
        list_headers = self._headers("list0001")
        list_response = await self.api.handle(
            "GET",
            _LIST_TARGET,
            list_headers,
        )
        self.assertEqual(list_response.status, 200)
        self._assert_attempt_headers(list_response.headers, list_headers)
        list_payload = _payload(list_response.body)
        self.validator.validate(
            "schemas/product-experience/agent-interaction-page.schema.json",
            list_payload,
        )
        self.assertEqual(list_response.headers["X-Interaction-High-Watermark"], "1")
        self.assertNotIn("ETag", list_response.headers)
        self.assertNotIn("X-Interaction-Revision", list_response.headers)
        self.assertEqual(
            self.application.list_calls,
            [(self.actor, _SESSION_ID, 0, 50)],
        )
        origin = cast(dict[str, object], list_payload["request_context"])
        self.assertNotEqual(origin["request_id"], list_headers["X-Request-Id"])
        self.assertNotEqual(origin["trace_id"], list_headers["X-Trace-Id"])

        get_headers = self._headers("get00002")
        get_response = await self.api.handle("GET", _GET_TARGET, get_headers)
        self.assertEqual(get_response.status, 200)
        self._assert_attempt_headers(get_response.headers, get_headers)
        get_payload = _payload(get_response.body)
        self.validator.validate(
            "schemas/product-experience/agent-interaction.schema.json",
            get_payload,
        )
        self.assertEqual(get_response.headers["X-Interaction-Revision"], "1")
        self.assertEqual(
            get_response.headers["ETag"],
            f'"interaction:1:{canonical_json_sha256(get_payload)}"',
        )
        self.assertNotIn("X-Interaction-High-Watermark", get_response.headers)
        self.assertEqual(
            self.application.get_calls,
            [(self.actor, _SESSION_ID, _INTERACTION_ID)],
        )

    async def test_explicit_limit_is_parsed_and_echoed(self) -> None:
        self.application.list_result = _list_result(_valid_page(requested_limit=1))
        headers = self._headers("limit001")
        response = await self.api.handle(
            "GET",
            _LIST_TARGET + "&limit=1",
            headers,
        )
        self.assertEqual(response.status, 200, _payload(response.body))
        self.assertEqual(
            self.application.list_calls,
            [(self.actor, _SESSION_ID, 0, 1)],
        )
        self.assertEqual(_payload(response.body)["requested_limit"], 1)

    async def test_attempt_schema_parse_and_authentication_precedence(self) -> None:
        complete = self._headers("preced01")

        missing_attempt = dict(complete)
        del missing_attempt["X-Request-Id"]
        response = await self.api.handle("GET", _LIST_TARGET, missing_attempt)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers["Content-Length"], "0")
        self.assertEqual(response.headers["Connection"], "close")
        self.assertNotIn("X-Request-Id", response.headers)

        malformed_attempt = dict(complete)
        malformed_attempt["X-Trace-Id"] = "not-a-trace"
        response = await self.api.handle("GET", _LIST_TARGET, malformed_attempt)
        self.assertEqual((response.status, response.body), (400, b""))
        self.assertEqual(response.headers["Connection"], "close")

        transport_attempt = dict(complete)
        transport_attempt["X-YaYa-Transport-Invalid"] = "ATTEMPT_IDENTITY_INVALID"
        response = await self.api.handle("GET", _LIST_TARGET, transport_attempt)
        self.assertEqual((response.status, response.body), (400, b""))

        framing = dict(complete)
        framing["X-YaYa-Transport-Invalid"] = "INVALID_FRAMING"
        response = await self.api.handle("GET", _LIST_TARGET, framing)
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            complete,
            expected_status=400,
            expected_code="INVALID_REQUEST",
        )

        schema_first = dict(complete)
        del schema_first["X-Schema-Version"]
        del schema_first["Authorization"]
        response = await self.api.handle(
            "GET",
            _LIST_TARGET.replace("after_sequence=0", "invalid=1"),
            schema_first,
        )
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            schema_first,
            expected_status=409,
            expected_code="SCHEMA_VERSION_UNSUPPORTED",
        )

        parse_first = dict(complete)
        del parse_first["Authorization"]
        response = await self.api.handle(
            "GET",
            _LIST_TARGET.replace("after_sequence=0", "invalid=1"),
            parse_first,
        )
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            parse_first,
            expected_status=400,
            expected_code="INVALID_REQUEST",
        )

        response = await self.api.handle("GET", _LIST_TARGET, parse_first)
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            parse_first,
            expected_status=401,
            expected_code="AUTHENTICATION_REQUIRED",
        )
        self.assertEqual(self.application.list_calls, [])

    async def test_strict_query_path_method_and_body_rejection(self) -> None:
        invalid_targets = (
            _LIST_TARGET.split("?", 1)[0],
            _LIST_TARGET.replace("after_sequence=0", "limit=1"),
            _LIST_TARGET + "&after_sequence=0",
            _LIST_TARGET + "&limit=1&limit=1",
            _LIST_TARGET + "&unknown=1",
            _LIST_TARGET.replace("after_sequence=0", "after_sequence="),
            _LIST_TARGET.replace("after_sequence=0", "after_sequence=+1"),
            _LIST_TARGET.replace("after_sequence=0", "after_sequence=%2B1"),
            _LIST_TARGET.replace("after_sequence=0", "after_sequence=%ZZ"),
            _LIST_TARGET.replace("after_sequence=0", "after_sequence=01"),
            _LIST_TARGET.replace("after_sequence=0", "after_sequence=9007199254740992"),
            _LIST_TARGET + "&limit=0",
            _LIST_TARGET + "&limit=101",
            _LIST_TARGET.replace("after_sequence=0", "after_sequence=0;limit=1"),
            _GET_TARGET + "?after_sequence=0",
            _LIST_TARGET + "#fragment",
        )
        for index, target in enumerate(invalid_targets):
            with self.subTest(index=index, target=target):
                headers = self._headers(f"query{index:04d}")
                response = await self.api.handle("GET", target, headers)
                self._assert_error(
                    response.status,
                    response.headers,
                    response.body,
                    headers,
                    expected_status=400,
                    expected_code="INVALID_REQUEST",
                )

        for method, body in (("POST", b""), ("PUT", b""), ("GET", b"{}")):
            with self.subTest(method=method, body=body):
                headers = self._headers(f"method{method.lower()}01")
                response = await self.api.handle(method, _LIST_TARGET, headers, body)
                self._assert_error(
                    response.status,
                    response.headers,
                    response.body,
                    headers,
                    expected_status=400,
                    expected_code="INVALID_REQUEST",
                )

        headers = self._headers("unknown1")
        response = await self.api.handle(
            "GET",
            "/product-experience/v1/sessions/session_agent_001/unknown",
            headers,
        )
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            headers,
            expected_status=404,
            expected_code="NOT_FOUND",
        )
        self.assertEqual(self.application.list_calls, [])
        self.assertEqual(self.application.get_calls, [])

    async def test_complete_product_error_status_code_and_header_matrix(self) -> None:
        cases: tuple[tuple[str, int, Mapping[str, object]], ...] = (
            ("INVALID_REQUEST", 400, {}),
            ("AUTHENTICATION_REQUIRED", 401, {}),
            ("AUTHORIZATION_DENIED", 403, {}),
            ("POLICY_DENIED", 403, {}),
            ("NOT_FOUND", 404, {}),
            ("SCHEMA_VERSION_UNSUPPORTED", 409, {}),
            ("CONTENT_VERSION_MISMATCH", 409, {}),
            ("IDEMPOTENCY_KEY_REUSED", 409, {}),
            ("RATE_LIMITED", 429, {"retry_after_seconds": 7}),
            ("INVARIANT_VIOLATION", 500, {}),
            ("INTERNAL_ERROR", 500, {}),
            ("DEPENDENCY_UNAVAILABLE", 503, {}),
        )
        operations = (
            ("list", _LIST_TARGET, "list_error"),
            ("get", _GET_TARGET, "get_error"),
        )
        for operation, target, error_field in operations:
            for index, (code, status, details) in enumerate(cases):
                with self.subTest(operation=operation, code=code):
                    application = _FakeApplication()
                    setattr(
                        application,
                        error_field,
                        ProductApplicationError(
                            code,
                            status,
                            "PRODUCT_READ",
                            f"injected {code}",
                            details,
                        ),
                    )
                    api = ProductHttpApi(
                        application=application,
                        authenticator=self.authenticator,
                        validator=self.validator,
                    )
                    headers = self._headers(f"{operation}{index:04d}")
                    response = await api.handle("GET", target, headers)
                    payload = self._assert_error(
                        response.status,
                        response.headers,
                        response.body,
                        headers,
                        expected_status=status,
                        expected_code=code,
                    )
                    self.assertEqual(
                        payload["status"],
                        "FAILED" if status >= 500 else "REJECTED",
                    )
                    if status == 429:
                        self.assertEqual(response.headers["Retry-After"], "7")
                    elif status == 503:
                        self.assertEqual(response.headers["Retry-After"], "1")
                    else:
                        self.assertNotIn("Retry-After", response.headers)

    async def test_rate_limit_retry_after_default_and_malformed_values_are_normalized(self) -> None:
        application = _FakeApplication()
        api = ProductHttpApi(
            application=application,
            authenticator=self.authenticator,
            validator=self.validator,
        )

        application.list_error = ProductApplicationError(
            "RATE_LIMITED",
            429,
            "PRODUCT_READ",
            "rate limited without explicit delay",
        )
        headers = self._headers("retrydef")
        response = await api.handle("GET", _LIST_TARGET, headers)
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            headers,
            expected_status=429,
            expected_code="RATE_LIMITED",
        )
        self.assertEqual(response.headers["Retry-After"], "1")

        for index, value in enumerate((False, 0, -1, "7", 1.5)):
            with self.subTest(value=value):
                application.list_error = ProductApplicationError(
                    "RATE_LIMITED",
                    429,
                    "PRODUCT_READ",
                    "rate limited with malformed delay",
                    {"retry_after_seconds": value},
                )
                attempt = self._headers(f"retry{index:03d}")
                response = await api.handle("GET", _LIST_TARGET, attempt)
                self._assert_error(
                    response.status,
                    response.headers,
                    response.body,
                    attempt,
                    expected_status=500,
                    expected_code="INTERNAL_ERROR",
                )
                self.assertNotIn("Retry-After", response.headers)

        application.list_error = ProductApplicationError(
            "RATE_LIMITED",
            429,
            "PRODUCT_READ",
            "rate limited with non-JSON details",
            {"untrusted": object()},
        )
        attempt = self._headers("badjson1")
        response = await api.handle("GET", _LIST_TARGET, attempt)
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            attempt,
            expected_status=500,
            expected_code="INTERNAL_ERROR",
        )
        self.assertNotIn("Retry-After", response.headers)

        application.list_error = ProductApplicationError(
            "EVENT_SEQUENCE_GAP",
            409,
            "PRODUCT_READ",
            "Game-only code leaked into Product",
        )
        attempt = self._headers("badcode1")
        response = await api.handle("GET", _LIST_TARGET, attempt)
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            attempt,
            expected_status=500,
            expected_code="INTERNAL_ERROR",
        )

    async def test_untrusted_get_projection_faults_are_suppressed(self) -> None:
        def schema_drift(value: dict[str, object]) -> None:
            value["untrusted_marker"] = "MUST_NOT_ESCAPE"

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

        def hash_drift(value: dict[str, object]) -> None:
            feedback = cast(dict[str, object], value["feedback"])
            feedback["message"] = "unhashed mutation MUST_NOT_ESCAPE"

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
            ("hash", hash_drift),
            ("link", link_drift),
        )
        for index, (name, mutate) in enumerate(mutations):
            with self.subTest(drift=name):
                interaction = _valid_interaction()
                mutate(interaction)
                application = _FakeApplication()
                application.get_result = _get_result(interaction)
                api = ProductHttpApi(
                    application=application,
                    authenticator=self.authenticator,
                    validator=self.validator,
                )
                headers = self._headers(f"getfault{index:02d}")
                response = await api.handle("GET", _GET_TARGET, headers)
                payload = self._assert_error(
                    response.status,
                    response.headers,
                    response.body,
                    headers,
                    expected_status=500,
                    expected_code="INVARIANT_VIOLATION",
                )
                self.assertNotEqual(payload, interaction)
                self.assertNotIn(b"MUST_NOT_ESCAPE", response.body)

        application = _FakeApplication()
        application.get_result = _get_result(
            _valid_interaction(),
            etag='"interaction:1:' + "0" * 64 + '"',
        )
        api = ProductHttpApi(
            application=application,
            authenticator=self.authenticator,
            validator=self.validator,
        )
        headers = self._headers("badetag1")
        response = await api.handle("GET", _GET_TARGET, headers)
        self._assert_error(
            response.status,
            response.headers,
            response.body,
            headers,
            expected_status=500,
            expected_code="INVARIANT_VIOLATION",
        )

        valid_result = _get_result(_valid_interaction())
        header_cases: dict[str, dict[str, str]] = {
            "missing_revision": {"ETag": valid_result.headers["ETag"]},
            "drifted_revision": {
                "ETag": valid_result.headers["ETag"],
                "X-Interaction-Revision": "2",
            },
            "missing_etag": {"X-Interaction-Revision": "1"},
            "extra_header": {
                **valid_result.headers,
                "X-Untrusted-Resource-Header": "MUST_NOT_ESCAPE",
            },
        }
        for index, (name, resource_headers) in enumerate(header_cases.items()):
            with self.subTest(resource_header=name):
                application = _FakeApplication()
                application.get_result = ProductReadResult(
                    dict(valid_result.payload),
                    resource_headers,
                )
                api = ProductHttpApi(
                    application=application,
                    authenticator=self.authenticator,
                    validator=self.validator,
                )
                headers = self._headers(f"hdrfault{index:02d}")
                response = await api.handle("GET", _GET_TARGET, headers)
                self._assert_error(
                    response.status,
                    response.headers,
                    response.body,
                    headers,
                    expected_status=500,
                    expected_code="INVARIANT_VIOLATION",
                )
                self.assertNotIn("ETag", response.headers)
                self.assertNotIn("X-Interaction-Revision", response.headers)
                self.assertNotIn("X-Untrusted-Resource-Header", response.headers)

    async def test_untrusted_page_and_high_watermark_faults_are_suppressed(self) -> None:
        def page_actor_drift(value: dict[str, object]) -> None:
            context = cast(dict[str, object], value["request_context"])
            actor = cast(dict[str, object], context["actor"])
            actor["actor_id"] = "student_other_001"

        def page_content_drift(value: dict[str, object]) -> None:
            context = cast(dict[str, object], value["request_context"])
            content = cast(dict[str, object], context["content_ref"])
            content["content_hash"] = "b" * 64

        def page_session_drift(value: dict[str, object]) -> None:
            value["session_id"] = "session_other_001"

        def page_cursor_drift(value: dict[str, object]) -> None:
            value["requested_after_sequence"] = 1

        def page_high_watermark_drift(value: dict[str, object]) -> None:
            value["high_watermark_sequence"] = 0

        def page_schema_drift(value: dict[str, object]) -> None:
            value["untrusted_marker"] = "MUST_NOT_ESCAPE"

        mutations: tuple[tuple[str, Callable[[dict[str, object]], None]], ...] = (
            ("actor", page_actor_drift),
            ("content", page_content_drift),
            ("session", page_session_drift),
            ("page", page_cursor_drift),
            ("high-watermark", page_high_watermark_drift),
            ("schema", page_schema_drift),
        )
        for index, (name, mutate) in enumerate(mutations):
            with self.subTest(drift=name):
                page = _valid_page()
                mutate(page)
                application = _FakeApplication()
                application.list_result = _list_result(page)
                api = ProductHttpApi(
                    application=application,
                    authenticator=self.authenticator,
                    validator=self.validator,
                )
                headers = self._headers(f"pagefault{index:02d}")
                response = await api.handle("GET", _LIST_TARGET, headers)
                payload = self._assert_error(
                    response.status,
                    response.headers,
                    response.body,
                    headers,
                    expected_status=500,
                    expected_code="INVARIANT_VIOLATION",
                )
                self.assertNotEqual(payload, page)
                self.assertNotIn(b"MUST_NOT_ESCAPE", response.body)

        valid_result = _list_result(_valid_page())
        list_header_cases: dict[str, dict[str, str]] = {
            "drifted_high_watermark": {"X-Interaction-High-Watermark": "999"},
            "missing_high_watermark": {},
            "extra_etag": {
                **valid_result.headers,
                "ETag": '"interaction:1:' + "0" * 64 + '"',
            },
            "extra_revision": {
                **valid_result.headers,
                "X-Interaction-Revision": "1",
            },
            "extra_header": {
                **valid_result.headers,
                "X-Untrusted-Resource-Header": "MUST_NOT_ESCAPE",
            },
        }
        for index, (name, resource_headers) in enumerate(list_header_cases.items()):
            with self.subTest(list_resource_header=name):
                application = _FakeApplication()
                application.list_result = ProductReadResult(
                    dict(valid_result.payload),
                    resource_headers,
                )
                api = ProductHttpApi(
                    application=application,
                    authenticator=self.authenticator,
                    validator=self.validator,
                )
                headers = self._headers(f"listhdr{index:02d}")
                response = await api.handle("GET", _LIST_TARGET, headers)
                self._assert_error(
                    response.status,
                    response.headers,
                    response.body,
                    headers,
                    expected_status=500,
                    expected_code="INVARIANT_VIOLATION",
                )
                self.assertNotIn("X-Untrusted-Resource-Header", response.headers)


if __name__ == "__main__":
    unittest.main()
