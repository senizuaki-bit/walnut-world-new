from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    SESSION_ID,
    TASK_ID,
    WORLD_ID,
    make_operation,
    make_session,
    make_skill,
    make_task,
    make_versions,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend import http_api  # noqa: E402
from yaya_agent_backend.application import AgentTurnApplication  # noqa: E402
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.codec import encode  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.http_api import (  # noqa: E402
    AgentHttpApi,
    HttpResponse,
    serve_http,
)
from yaya_agent_backend.http_router import ProductionHttpApi  # noqa: E402
from yaya_agent_backend.product_application import (  # noqa: E402
    ProductInteractionReadApplication,
)
from yaya_agent_backend.product_http_api import ProductHttpApi  # noqa: E402
from yaya_agent_backend.product_repositories import (  # noqa: E402
    PostgresProductInteractionReadRepository,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActiveSkill,
    BuildArtifact,
    CertifiedSkill,
    OperationContext,
    RequestContext,
    canonical_json_sha256,
)


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
    )


def _body() -> dict[str, object]:
    skill = make_skill().ref
    return {
        "turn_id": "turn_http_transport_0001",
        "expected_world_revision": 5,
        "input": {
            "type": "MESSAGE",
            "text": "Water every plot exactly once.",
            "locale": "en-US",
        },
        "skill_bindings": [
            {
                "skill_id": skill.skill_id,
                "skill_version_id": skill.skill_version_id,
                "artifact_sha256": skill.artifact_sha256,
                "certification_id": skill.certification_id,
            }
        ],
        "client_state": {
            "last_event_sequence": 40,
            "client_turn_sequence": 1,
        },
    }


def _json_body(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _exchange(
    port: int,
    request: bytes,
    *,
    require_eof: bool = False,
) -> tuple[int, list[tuple[str, str]], bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.settimeout(10)
        connection.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                raise AssertionError("HTTP server closed before sending response headers")
            response.extend(chunk)
        header_bytes, body_prefix = bytes(response).split(b"\r\n\r\n", 1)
        lines = header_bytes.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        headers: list[tuple[str, str]] = []
        content_length: int | None = None
        for line in lines[1:]:
            name, value = line.split(":", 1)
            normalized = name.strip().lower()
            stripped = value.strip()
            headers.append((normalized, stripped))
            if normalized == "content-length":
                content_length = int(stripped)
        if content_length is None:
            raise AssertionError("HTTP response omitted Content-Length")
        response_body = bytearray(body_prefix)
        while len(response_body) < content_length:
            chunk = connection.recv(content_length - len(response_body))
            if not chunk:
                raise AssertionError("HTTP server closed before sending the declared body")
            response_body.extend(chunk)
        if require_eof and connection.recv(1) != b"":
            raise AssertionError("HTTP framing rejection did not close the connection")
        return status, headers, bytes(response_body[:content_length])


def _request(
    headers: list[tuple[str, str]],
    body: bytes = b"",
    *,
    connection_close: bool = True,
) -> bytes:
    return _method_request(
        "POST",
        f"/v1/agent-sessions/{SESSION_ID}/turns",
        headers,
        body,
        connection_close=connection_close,
    )


def _method_request(
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes = b"",
    *,
    connection_close: bool = True,
) -> bytes:
    head = [
        f"{method} {target} HTTP/1.1",
        "Host: 127.0.0.1",
        *(f"{name}: {value}" for name, value in headers),
        *(("Connection: close",) if connection_close else ()),
        "",
        "",
    ]
    return "\r\n".join(head).encode("iso-8859-1") + body


class _FailFirstAcceptedSerializationApi(AgentHttpApi):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._failure_lock = threading.Lock()
        self._fail_next_accepted = False

    def arm_accepted_failure(self) -> None:
        with self._failure_lock:
            self._fail_next_accepted = True

    def _success(
        self,
        status: int,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> HttpResponse:
        with self._failure_lock:
            fail = status == 202 and self._fail_next_accepted
            if fail:
                self._fail_next_accepted = False
        if fail:
            raise TypeError("injected accepted-response serialization failure")
        return super()._success(status, payload, headers)


class AgentBackendHttpTransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server_context = postgres_test_server()
        cls._http_server: Any | None = None
        cls.http_thread: threading.Thread | None = None
        cls._original_server_factory = http_api.ThreadingHTTPServer
        try:
            cls.server = cls._server_context.__enter__()
            cls.database = PostgresDatabase(cls.server.dsn)
            asyncio.run(cls.database.migrate())
            cls.origin = make_operation()
            cls.authenticator = JwtAuthenticator(
                hmac_secret="http-transport-test-secret-0000000000000000",
                issuer="yaya-http-transport-test",
                audience="yaya-agent-test",
            )
            cls.token = cls.authenticator.issue_for_test(
                cls.origin.actor,
                now=datetime.now(UTC),
            )
            cls.validator = ContractSchemaValidator(CONTRACTS_ROOT)
            cls.api = _FailFirstAcceptedSerializationApi(
                application=AgentTurnApplication(
                    cls.database,
                    CONTRACTS_ROOT,
                    make_versions(),
                ),
                authenticator=cls.authenticator,
                validator=cls.validator,
            )
            cls.product_repository = PostgresProductInteractionReadRepository(
                cls.database,
                cls.validator,
            )
            cls.product_application = ProductInteractionReadApplication(
                cls.product_repository,
                cls.validator,
            )
            cls.product_api = ProductHttpApi(
                application=cls.product_application,
                authenticator=cls.authenticator,
                validator=cls.validator,
            )
            cls.production_api = ProductionHttpApi(
                game=cls.api,
                product=cls.product_api,
            )
            captured = threading.Event()

            def capture_server(*args: object, **kwargs: object) -> Any:
                server = cast(Any, cls._original_server_factory)(*args, **kwargs)
                cls._http_server = server
                captured.set()
                return server

            http_api.ThreadingHTTPServer = cast(Any, capture_server)
            ready = threading.Event()
            cls.http_thread = threading.Thread(
                target=serve_http,
                args=(cls.production_api, "127.0.0.1", 0),
                kwargs={"ready": ready},
                daemon=True,
                name="yaya-http-transport-test-server",
            )
            cls.http_thread.start()
            if (
                not captured.wait(timeout=10)
                or not ready.wait(timeout=10)
                or cls._http_server is None
            ):
                raise RuntimeError("real HTTP test server did not become ready")
            cls.port = int(cls._http_server.server_address[1])
        except BaseException:
            http_api.ThreadingHTTPServer = cls._original_server_factory
            if cls._http_server is not None:
                cls._http_server.shutdown()
            if cls.http_thread is not None:
                cls.http_thread.join(timeout=5)
            cls._server_context.__exit__(*sys.exc_info())
            raise
        finally:
            http_api.ThreadingHTTPServer = cls._original_server_factory

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._http_server is not None:
            cls._http_server.shutdown()
        if cls.http_thread is not None:
            cls.http_thread.join(timeout=10)
            if cls.http_thread.is_alive():
                raise RuntimeError("real HTTP test server did not stop")
        cls._server_context.__exit__(None, None, None)

    async def asyncSetUp(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                TRUNCATE yaya_agent_turns,yaya_agent_interactions,
                  yaya_projection_outbox,yaya_agent_messages,yaya_events,
                  yaya_command_jobs,yaya_runs,yaya_commands,
                  yaya_registry_active,yaya_registry_certifications,
                  yaya_skills,yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()
        await self._seed_authority(self.origin)

    async def _seed_authority(self, context: OperationContext) -> None:
        task = make_task(context)
        session = make_session(operation=context)
        skill = make_skill(context)
        state = make_world_state()
        artifact = BuildArtifact(
            artifact_sha256=skill.ref.artifact_sha256,
            source_sha256=skill.source_sha256,
            compiler_profile="gcc-cpp20",
            compiler_version="gcc 15",
            sandbox_image_digest="gcc@sha256:" + "c" * 64,
            test_suite_version="watering-1",
            artifact_uri="file:///http-transport-test/skill",
        )
        certified = CertifiedSkill(
            certification_id=skill.ref.certification_id,
            skill_id=skill.ref.skill_id,
            skill_version_id=skill.ref.skill_version_id,
            semantic_version="1.0.0",
            artifact=artifact,
            capabilities=("WORLD_READ", "WATER"),
            certified_at=NOW,
            revoked_at=None,
        )
        active = ActiveSkill(certified, 1, NOW)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(tenant_id,task_id,actor_id,content_hash,snapshot_json)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    TASK_ID,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(task)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                  tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                  last_event_sequence,state_hash,world_rules_version,state_json,
                  request_context_json
                ) VALUES (%s,%s,%s,%s,%s,5,40,%s,'farm-rules-1',%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    WORLD_ID,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    f"world:{WORLD_ID}",
                    canonical_json_sha256(state),
                    Jsonb(state),
                    Jsonb(encode(_request_context(context))),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_agent_sessions(
                  tenant_id,session_id,actor_id,task_id,world_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    SESSION_ID,
                    context.actor.actor_id,
                    TASK_ID,
                    WORLD_ID,
                    context.content_ref.content_hash,
                    Jsonb(encode(session)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_skills(
                  tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                  session_id,content_hash,artifact_sha256,snapshot_json,active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                """,
                (
                    context.actor.tenant_id,
                    skill.ref.skill_id,
                    skill.ref.skill_version_id,
                    skill.ref.certification_id,
                    context.actor.actor_id,
                    SESSION_ID,
                    context.content_ref.content_hash,
                    skill.ref.artifact_sha256,
                    Jsonb(encode(skill)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_registry_certifications(
                  tenant_id,certification_id,skill_id,skill_version_id,
                  artifact_sha256,record_json,rejected
                ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                """,
                (
                    context.actor.tenant_id,
                    certified.certification_id,
                    certified.skill_id,
                    certified.skill_version_id,
                    certified.artifact.artifact_sha256,
                    Jsonb(encode(certified)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_registry_active(
                  tenant_id,actor_id,skill_id,record_json,revision
                ) VALUES (%s,%s,%s,%s,1)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    certified.skill_id,
                    Jsonb(encode(active)),
                ),
            )
        finally:
            await connection.close()

    def _valid_headers(self, body: bytes) -> list[tuple[str, str]]:
        return [
            ("Authorization", f"Bearer {self.token}"),
            ("X-Schema-Version", "1.0.0"),
            ("X-Request-Id", "req_http_transport_0001"),
            ("X-Trace-Id", "trace_http_transport_0001"),
            ("X-Correlation-Id", "corr_http_transport_0001"),
            ("Idempotency-Key", "agent-turn:http-transport:0001"),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ]

    def _product_headers(self) -> list[tuple[str, str]]:
        return [
            ("Authorization", f"Bearer {self.token}"),
            ("X-Schema-Version", "1.0.0"),
            ("X-Request-Id", "req_product_transport_0001"),
            ("X-Trace-Id", "trace_product_transport_0001"),
            ("X-Correlation-Id", "corr_product_transport_0001"),
        ]

    async def _business_fingerprint(self) -> tuple[tuple[str, str], ...]:
        connection = await self.database.connect(autocommit=True)
        try:
            table_cursor = await connection.execute(
                """
                SELECT schemaname,tablename FROM pg_catalog.pg_tables
                WHERE schemaname=ANY(current_schemas(false))
                  AND left(tablename,5)='yaya_'
                ORDER BY schemaname,tablename
                """
            )
            table_rows = list(await table_cursor.fetchall())
            result: list[tuple[str, str]] = []
            for table_row in table_rows:
                schema_name = table_row["schemaname"]
                table_name = table_row["tablename"]
                if not isinstance(schema_name, str) or not isinstance(table_name, str):
                    self.fail("PostgreSQL returned an invalid business table identity")
                query = sql.SQL(
                    "SELECT COALESCE("
                    "jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                    "'[]'::jsonb) AS rows FROM {}.{} AS t"
                ).format(sql.Identifier(schema_name), sql.Identifier(table_name))
                cursor = await connection.execute(query)
                row = await cursor.fetchone()
                if row is None:
                    self.fail(f"fingerprint query for {schema_name}.{table_name} returned no row")
                result.append(
                    (
                        f"{schema_name}.{table_name}",
                        canonical_json_sha256({"rows": row["rows"]}),
                    )
                )
            if not result:
                self.fail("PostgreSQL exposed no yaya_* business tables")
            return tuple(result)
        finally:
            await connection.close()

    @staticmethod
    def _product_list_target() -> str:
        return f"/product-experience/v1/sessions/{SESSION_ID}/agent-interactions?after_sequence=0"

    async def test_product_localhost_auth_schema_scope_and_validation_failures_do_not_write(
        self,
    ) -> None:
        before = await self._business_fingerprint()
        base = self._product_headers()

        missing_attempt = [item for item in base if item[0].lower() != "x-request-id"]
        status, response_headers, response_body = await asyncio.to_thread(
            _exchange,
            self.port,
            _method_request("GET", self._product_list_target(), missing_attempt),
        )
        self.assertEqual((status, response_body), (400, b""))
        self.assertNotIn("x-request-id", dict(response_headers))

        missing_auth = [item for item in base if item[0].lower() != "authorization"]
        invalid_auth = [
            (name, "Bearer invalid-token" if name.lower() == "authorization" else value)
            for name, value in base
        ]
        missing_schema = [item for item in base if item[0].lower() != "x-schema-version"]
        wrong_schema = [
            (name, "9.9.9" if name.lower() == "x-schema-version" else value) for name, value in base
        ]
        cases: tuple[
            tuple[str, str, str, list[tuple[str, str]], bytes, int, str, bool],
            ...,
        ] = (
            (
                "missing_auth",
                "GET",
                self._product_list_target(),
                missing_auth,
                b"",
                401,
                "AUTHENTICATION_REQUIRED",
                False,
            ),
            (
                "invalid_auth",
                "GET",
                self._product_list_target(),
                invalid_auth,
                b"",
                401,
                "AUTHENTICATION_REQUIRED",
                False,
            ),
            (
                "missing_schema",
                "GET",
                self._product_list_target(),
                missing_schema,
                b"",
                409,
                "SCHEMA_VERSION_UNSUPPORTED",
                False,
            ),
            (
                "wrong_schema",
                "GET",
                self._product_list_target(),
                wrong_schema,
                b"",
                409,
                "SCHEMA_VERSION_UNSUPPORTED",
                False,
            ),
            (
                "missing_session",
                "GET",
                "/product-experience/v1/sessions/session_missing_0001/"
                "agent-interactions?after_sequence=0",
                base,
                b"",
                404,
                "NOT_FOUND",
                False,
            ),
            (
                "missing_interaction",
                "GET",
                f"/product-experience/v1/sessions/{SESSION_ID}/"
                "agent-interactions/interaction_missing_0001",
                base,
                b"",
                404,
                "NOT_FOUND",
                False,
            ),
            (
                "invalid_query",
                "GET",
                self._product_list_target() + "&unknown=1",
                base,
                b"",
                400,
                "INVALID_REQUEST",
                False,
            ),
            (
                "body",
                "GET",
                self._product_list_target(),
                [*base, ("Content-Length", "2")],
                b"{}",
                400,
                "INVALID_REQUEST",
                True,
            ),
            (
                "oversized_declared_body",
                "GET",
                self._product_list_target(),
                [*base, ("Content-Length", str(8 * 1024 * 1024 + 1))],
                b"",
                400,
                "INVALID_REQUEST",
                True,
            ),
            (
                "method",
                "PUT",
                self._product_list_target(),
                base,
                b"",
                400,
                "INVALID_REQUEST",
                False,
            ),
        )
        for name, method, target, headers, body, expected_status, code, require_eof in cases:
            with self.subTest(case=name):
                status, response_headers, response_body = await asyncio.to_thread(
                    _exchange,
                    self.port,
                    _method_request(method, target, headers, body),
                    require_eof=require_eof,
                )
                self.assertEqual(status, expected_status)
                payload = json.loads(response_body)
                self.assertEqual(payload["error"]["code"], code)
                header_map = dict(response_headers)
                self.assertEqual(
                    header_map["x-request-id"],
                    "req_product_transport_0001",
                )
                self.assertEqual(
                    header_map["x-trace-id"],
                    "trace_product_transport_0001",
                )
                self.assertEqual(
                    header_map["x-correlation-id"],
                    "corr_product_transport_0001",
                )

        self.assertEqual(await self._business_fingerprint(), before)

    def test_product_attempt_identity_failures_are_bare_and_close_connection(
        self,
    ) -> None:
        base = self._product_headers()
        duplicate_values = {
            "x-request-id": "req_product_transport_duplicate_0002",
            "x-trace-id": "trace_product_transport_duplicate_0002",
            "x-correlation-id": "corr_product_transport_duplicate_0002",
        }
        cases: dict[str, list[tuple[str, str]]] = {}
        for normalized, duplicate_value in duplicate_values.items():
            cases[f"missing_{normalized}"] = [
                item for item in base if item[0].lower() != normalized
            ]
            cases[f"duplicate_{normalized}"] = [
                *base,
                (normalized.swapcase(), duplicate_value),
            ]
            cases[f"malformed_{normalized}"] = [
                (name, "bad" if name.lower() == normalized else value) for name, value in base
            ]

        attempt_names = {
            "x-request-id",
            "x-trace-id",
            "x-correlation-id",
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                status, response_headers, response_body = _exchange(
                    self.port,
                    _method_request(
                        "GET",
                        self._product_list_target(),
                        headers,
                        connection_close=False,
                    ),
                    require_eof=True,
                )
                self.assertEqual(status, 400)
                self.assertEqual(response_body, b"")
                header_map = dict(response_headers)
                self.assertEqual(header_map["content-length"], "0")
                self.assertEqual(header_map["cache-control"], "no-store")
                self.assertEqual(header_map["connection"], "close")
                self.assertTrue(attempt_names.isdisjoint(header_map))

    def test_product_duplicate_singletons_are_closed_json_framing_errors(
        self,
    ) -> None:
        base = self._product_headers()
        cases = {
            "authorization": [
                *base,
                ("authorization", f"Bearer {self.token}"),
            ],
            "schema_version": [
                *base,
                ("x-SCHEMA-version", "1.0.0"),
            ],
            "content_length": [
                *base,
                ("Content-Length", "0"),
                ("content-length", "0"),
            ],
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                status, response_headers, response_body = _exchange(
                    self.port,
                    _method_request(
                        "GET",
                        self._product_list_target(),
                        headers,
                        connection_close=False,
                    ),
                    require_eof=True,
                )
                self.assertEqual(status, 400)
                payload = json.loads(response_body)
                self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(payload["error"]["stage"], "PRODUCT_VALIDATE")
                header_map = dict(response_headers)
                self.assertEqual(
                    header_map["x-request-id"],
                    "req_product_transport_0001",
                )
                self.assertEqual(
                    header_map["x-trace-id"],
                    "trace_product_transport_0001",
                )
                self.assertEqual(
                    header_map["x-correlation-id"],
                    "corr_product_transport_0001",
                )

    def test_product_get_body_forces_framing_rejection_and_connection_close(
        self,
    ) -> None:
        headers = [*self._product_headers(), ("Content-Length", "2")]
        status, response_headers, response_body = _exchange(
            self.port,
            _method_request(
                "GET",
                self._product_list_target(),
                headers,
                b"{}",
                connection_close=False,
            ),
            require_eof=True,
        )
        self.assertEqual(status, 400)
        payload = json.loads(response_body)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(payload["error"]["stage"], "PRODUCT_VALIDATE")
        header_map = dict(response_headers)
        self.assertEqual(header_map["x-request-id"], "req_product_transport_0001")
        self.assertEqual(header_map["x-trace-id"], "trace_product_transport_0001")
        self.assertEqual(
            header_map["x-correlation-id"],
            "corr_product_transport_0001",
        )

    def test_product_unsupported_and_lowercase_methods_use_closed_json_errors(
        self,
    ) -> None:
        for method in ("PUT", "TRACE", "get"):
            with self.subTest(method=method):
                status, response_headers, response_body = _exchange(
                    self.port,
                    _method_request(
                        method,
                        self._product_list_target(),
                        self._product_headers(),
                    ),
                )
                self.assertEqual(status, 400)
                payload = json.loads(response_body)
                self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(payload["error"]["stage"], "PRODUCT_VALIDATE")
                header_map = dict(response_headers)
                self.assertEqual(
                    header_map["x-request-id"],
                    "req_product_transport_0001",
                )
                self.assertEqual(
                    header_map["x-trace-id"],
                    "trace_product_transport_0001",
                )
                self.assertEqual(
                    header_map["x-correlation-id"],
                    "corr_product_transport_0001",
                )

    async def test_duplicate_singletons_and_cl_te_smuggling_fail_before_acceptance(self) -> None:
        body = _json_body(_body())
        valid = self._valid_headers(body)
        cases = {
            "authorization": [*valid, ("Authorization", f"Bearer {self.token}")],
            "idempotency": [*valid, ("Idempotency-Key", "agent-turn:http-transport:0001")],
            "content_length": [*valid, ("Content-Length", str(len(body)))],
            "transfer_encoding": [
                *(item for item in valid if item[0].lower() != "content-length"),
                ("Transfer-Encoding", "chunked"),
            ],
            "cl_and_te": [*valid, ("Transfer-Encoding", "chunked")],
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                status, _, response_body = _exchange(
                    self.port,
                    _request(headers, body, connection_close=False),
                    require_eof=True,
                )
                self.assertEqual(status, 400)
                response = json.loads(response_body)
                self.assertEqual(response["error"]["code"], "INVALID_REQUEST")

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands) AS commands,
                  (SELECT count(*) FROM yaya_command_jobs) AS jobs,
                  (SELECT client_turn_sequence FROM yaya_agent_sessions) AS sequence
                """
            )
            counts = await cursor.fetchone()
        finally:
            await connection.close()
        if counts is None:
            self.fail("HTTP framing persistence query returned no row")
        self.assertEqual(
            (counts["commands"], counts["jobs"], counts["sequence"]),
            (0, 0, 0),
        )

    async def test_missing_attempt_identity_is_empty_rejection_without_minted_ids(self) -> None:
        body = _json_body(_body())
        for missing in ("x-request-id", "x-trace-id", "x-correlation-id"):
            with self.subTest(missing=missing):
                headers = [item for item in self._valid_headers(body) if item[0].lower() != missing]
                status, response_headers, response_body = _exchange(
                    self.port,
                    _request(headers, body, connection_close=False),
                    require_eof=True,
                )
                self.assertEqual(status, 400)
                self.assertEqual(response_body, b"")
                names = {name for name, _ in response_headers}
                self.assertFalse({"x-request-id", "x-trace-id", "x-correlation-id"} & names)
                self.assertIn(("connection", "close"), response_headers)

        await self._assert_no_accepted_turn()

    async def test_duplicate_attempt_identity_any_casing_is_empty_rejection(self) -> None:
        body = _json_body(_body())
        canonical = {
            "x-request-id": "X-Request-Id",
            "x-trace-id": "X-Trace-Id",
            "x-correlation-id": "X-Correlation-Id",
        }
        for normalized, header_name in canonical.items():
            for casing in ("same", "different"):
                with self.subTest(header=normalized, casing=casing):
                    duplicate_name = header_name if casing == "same" else normalized.swapcase()
                    headers = [
                        *self._valid_headers(body),
                        (duplicate_name, f"{normalized.replace('-', '_')}_duplicate_0002"),
                    ]
                    status, response_headers, response_body = _exchange(
                        self.port,
                        _request(headers, body, connection_close=False),
                        require_eof=True,
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(response_body, b"")
                    names = {name for name, _ in response_headers}
                    self.assertFalse(set(canonical) & names)
                    self.assertIn(("connection", "close"), response_headers)

        await self._assert_no_accepted_turn()

    async def test_malformed_attempt_identity_wins_over_framing_errors(self) -> None:
        body = _json_body(_body())
        attempt_headers = (
            "x-request-id",
            "x-trace-id",
            "x-correlation-id",
        )
        malformed_values = (
            "",
            "wrong_prefix_0001",
            "req_" + "x" * 97,
            "req_valid_0001,req_other_0002",
        )
        for normalized in attempt_headers:
            for malformed in malformed_values:
                with self.subTest(header=normalized, malformed=malformed[:20]):
                    headers = [
                        (name, malformed if name.lower() == normalized else value)
                        for name, value in self._valid_headers(body)
                    ]
                    status, response_headers, response_body = _exchange(
                        self.port,
                        _request(headers, body, connection_close=False),
                        require_eof=True,
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(response_body, b"")
                    names = {name for name, _ in response_headers}
                    self.assertFalse(set(attempt_headers) & names)
                    self.assertIn(("connection", "close"), response_headers)

        invalid_attempt = [
            (name, "bad" if name.lower() == "x-request-id" else value)
            for name, value in self._valid_headers(body)
        ]
        framing_cases = {
            "transfer_encoding": [*invalid_attempt, ("Transfer-Encoding", "chunked")],
            "duplicate_content_length": [
                *invalid_attempt,
                ("Content-Length", str(len(body))),
            ],
            "oversized": [
                (
                    name,
                    str(8 * 1024 * 1024 + 1) if name.lower() == "content-length" else value,
                )
                for name, value in invalid_attempt
            ],
        }
        for name, headers in framing_cases.items():
            with self.subTest(framing=name):
                status, response_headers, response_body = _exchange(
                    self.port,
                    _request(headers, body, connection_close=False),
                    require_eof=True,
                )
                self.assertEqual(status, 400)
                self.assertEqual(response_body, b"")
                names = {header for header, _ in response_headers}
                self.assertFalse(set(attempt_headers) & names)
                self.assertIn(("connection", "close"), response_headers)

        await self._assert_no_accepted_turn()

    async def _assert_no_accepted_turn(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands) AS commands,
                  (SELECT count(*) FROM yaya_command_jobs) AS jobs,
                  (SELECT client_turn_sequence FROM yaya_agent_sessions) AS sequence
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("attempt identity persistence query returned no row")
        self.assertEqual(
            (row["commands"], row["jobs"], row["sequence"]),
            (0, 0, 0),
        )

    async def test_missing_content_length_with_body_and_pipeline_closes_after_one_rejection(
        self,
    ) -> None:
        body = _json_body(_body())
        headers = [
            item for item in self._valid_headers(body) if item[0].lower() != "content-length"
        ]
        pipelined = _method_request(
            "GET",
            f"/v1/worlds/{WORLD_ID}/snapshot",
            [
                ("Authorization", f"Bearer {self.token}"),
                ("X-Schema-Version", "1.0.0"),
                ("X-Request-Id", "req_http_pipeline_0002"),
                ("X-Trace-Id", "trace_http_pipeline_0002"),
                ("X-Correlation-Id", "corr_http_pipeline_0002"),
            ],
        )
        status, _, response_body = _exchange(
            self.port,
            _request(
                headers,
                body + pipelined,
                connection_close=False,
            ),
            require_eof=True,
        )
        self.assertEqual(status, 400)
        response = json.loads(response_body)
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")

    async def test_content_length_grammar_rejects_a_signed_decimal(self) -> None:
        body = _json_body(_body())
        headers = [
            (name, f"+{len(body)}" if name.lower() == "content-length" else value)
            for name, value in self._valid_headers(body)
        ]
        status, _, response_body = _exchange(
            self.port,
            _request(headers, body),
        )
        self.assertEqual(status, 400)
        response = json.loads(response_body)
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute("SELECT count(*) AS value FROM yaya_commands")
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            self.fail("command-count query returned no row")
        self.assertEqual(row["value"], 0)

    async def test_oversized_declared_get_body_is_never_dispatched(self) -> None:
        request = _method_request(
            "GET",
            f"/v1/worlds/{WORLD_ID}/snapshot",
            [
                ("Authorization", f"Bearer {self.token}"),
                ("X-Schema-Version", "1.0.0"),
                ("X-Request-Id", "req_http_oversize_0001"),
                ("X-Trace-Id", "trace_http_oversize_0001"),
                ("X-Correlation-Id", "corr_http_oversize_0001"),
                ("Content-Length", str(8 * 1024 * 1024 + 1)),
            ],
            connection_close=False,
        )
        status, _, response_body = _exchange(
            self.port,
            request,
            require_eof=True,
        )
        self.assertEqual(status, 413)
        response = json.loads(response_body)
        self.assertEqual(response["error"]["code"], "PAYLOAD_TOO_LARGE")

    async def test_unimplemented_and_lowercase_methods_use_contract_json_errors(self) -> None:
        base_headers = [
            ("Authorization", f"Bearer {self.token}"),
            ("X-Schema-Version", "1.0.0"),
            ("X-Request-Id", "req_http_method_0001"),
            ("X-Trace-Id", "trace_http_method_0001"),
            ("X-Correlation-Id", "corr_http_method_0001"),
        ]
        for method in ("TRACE", "CONNECT", "post"):
            with self.subTest(method=method):
                status, response_headers, response_body = _exchange(
                    self.port,
                    _method_request(
                        method,
                        f"/v1/agent-sessions/{SESSION_ID}/turns",
                        base_headers,
                    ),
                )
                self.assertEqual(status, 400)
                response = json.loads(response_body)
                self.assertEqual(response["error"]["code"], "INVALID_REQUEST")
                header_map = dict(response_headers)
                self.assertEqual(header_map["x-request-id"], "req_http_method_0001")
                self.assertEqual(header_map["x-trace-id"], "trace_http_method_0001")

    async def test_durable_acceptance_serialization_loss_returns_unknown_then_replays(self) -> None:
        body = _json_body(_body())
        request = _request(self._valid_headers(body), body)
        self.api.arm_accepted_failure()

        first_status, first_headers, first_body = _exchange(self.port, request)
        self.assertEqual(first_status, 503)
        first_header_map = dict(first_headers)
        first_payload = json.loads(first_body)
        self.assertEqual(first_payload["status"], "UNKNOWN")
        self.assertEqual(first_payload["error"]["code"], "UNKNOWN_COMMIT_STATE")
        command_id = first_payload["command_id"]
        self.assertEqual(first_header_map["location"], f"/v1/commands/{command_id}")
        self.assertEqual(first_header_map["retry-after"], "1")

        replay_status, replay_headers, replay_body = _exchange(self.port, request)
        self.assertEqual(replay_status, 202)
        replay_header_map = dict(replay_headers)
        replay_payload = json.loads(replay_body)
        self.assertEqual(replay_payload["command_id"], command_id)
        self.assertEqual(replay_header_map["location"], f"/v1/commands/{command_id}")
        self.assertEqual(replay_header_map["idempotency-replayed"], "true")

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands) AS commands,
                  (SELECT count(*) FROM yaya_command_jobs) AS jobs,
                  (SELECT client_turn_sequence FROM yaya_agent_sessions) AS sequence
                """
            )
            counts = await cursor.fetchone()
        finally:
            await connection.close()
        if counts is None:
            self.fail("HTTP response-loss persistence query returned no row")
        self.assertEqual(
            (counts["commands"], counts["jobs"], counts["sequence"]),
            (1, 1, 1),
        )


if __name__ == "__main__":
    unittest.main()
