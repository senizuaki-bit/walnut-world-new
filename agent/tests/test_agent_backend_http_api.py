from __future__ import annotations

import asyncio
import http.client
import json
import sys
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
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
from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from yaya_agent_backend import http_api  # noqa: E402
from yaya_agent_backend.application import AgentTurnApplication  # noqa: E402
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.codec import decode_as, encode, plain  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.http_api import AgentHttpApi, serve_http  # noqa: E402
from yaya_agent_backend.invocation import PostgresSkillInvocationService  # noqa: E402
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_backend.world import WateringWorldEngine  # noqa: E402
from yaya_agent_backend.world_uow import PostgresWorldUnitOfWork  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActiveSkill,
    BuildArtifact,
    CertifiedSkill,
    ContractError,
    ErrorCategory,
    Failure,
    OperationContext,
    RequestContext,
    SandboxLimits,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    GameEvent,
    SkillInvocationRequest,
    skill_invocation_request_sha256,
)

_JWT_SECRET = "http-gate-secret-" + "d" * 48
_JWT_ISSUER = "https://identity.yaya.local"
_JWT_AUDIENCE = "yaya-game-api"


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
        "turn_id": "turn_http_gate_0001",
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
            "last_event_sequence": 0,
            "client_turn_sequence": 1,
        },
    }


def _raw(body: dict[str, object]) -> bytes:
    return json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _TimedOutSandbox:
    async def run(self, request: object, context: OperationContext) -> Failure:
        del request, context
        return Failure(
            ContractError(
                code="SANDBOX_RESOURCE_LIMIT",
                category=ErrorCategory.SANDBOX,
                retryable=False,
                user_message_key="sandbox.resource_limit",
                stage="SANDBOX",
                message="Injected bounded timeout for the HTTP read gate.",
                details={"reason": "WALL_TIMEOUT"},
            )
        )


class AgentBackendHttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._postgres_context = postgres_test_server()
        cls._server_thread: threading.Thread | None = None
        cls._http_server: Any | None = None
        cls._original_server_factory = http_api.ThreadingHTTPServer
        try:
            cls.postgres = cls._postgres_context.__enter__()
            cls.database = PostgresDatabase(cls.postgres.dsn)
            asyncio.run(cls.database.migrate())
            cls.validator = ContractSchemaValidator(CONTRACTS_ROOT)
            cls.authenticator = JwtAuthenticator(
                hmac_secret=_JWT_SECRET,
                issuer=_JWT_ISSUER,
                audience=_JWT_AUDIENCE,
            )
            cls.application = AgentTurnApplication(
                cls.database,
                CONTRACTS_ROOT,
                make_versions(),
            )
            api = AgentHttpApi(
                application=cls.application,
                authenticator=cls.authenticator,
                validator=cls.validator,
            )
            captured = threading.Event()

            def capture_server(*args: object, **kwargs: object) -> Any:
                server = cls._original_server_factory(*args, **kwargs)
                cls._http_server = server
                captured.set()
                return server

            http_api.ThreadingHTTPServer = cast(Any, capture_server)
            ready = threading.Event()
            cls._server_thread = threading.Thread(
                target=serve_http,
                args=(api, "127.0.0.1", 0),
                kwargs={"ready": ready},
                name="yaya-http-gate",
                daemon=True,
            )
            cls._server_thread.start()
            if not captured.wait(10) or not ready.wait(10) or cls._http_server is None:
                raise RuntimeError("loopback HTTP server did not become ready")
            cls.port = int(cls._http_server.server_address[1])
        except BaseException:
            http_api.ThreadingHTTPServer = cls._original_server_factory
            if cls._http_server is not None:
                cls._http_server.shutdown()
            if cls._server_thread is not None:
                cls._server_thread.join(timeout=5)
            cls._postgres_context.__exit__(*sys.exc_info())
            raise
        finally:
            http_api.ThreadingHTTPServer = cls._original_server_factory

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._http_server is not None:
            cls._http_server.shutdown()
        if cls._server_thread is not None:
            cls._server_thread.join(timeout=10)
            if cls._server_thread.is_alive():
                raise RuntimeError("loopback HTTP server did not stop")
        cls._postgres_context.__exit__(None, None, None)

    def setUp(self) -> None:
        self.origin = make_operation()
        asyncio.run(self._reset_and_seed(self.origin))
        self.token = self.authenticator.issue_for_test(
            self.origin.actor,
            now=datetime.now(UTC),
        )

    async def _reset_and_seed(self, context: OperationContext) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                TRUNCATE yaya_skill_invocations,yaya_runs,yaya_evidence,
                  yaya_projection_outbox,yaya_command_jobs,yaya_commands,
                  yaya_events,yaya_outbox,yaya_audit,
                  yaya_registry_active,yaya_registry_certifications,
                  yaya_skills,yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()

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
            artifact_uri="file:///http-gate/skill",
        )
        certified = CertifiedSkill(
            certification_id=skill.ref.certification_id,
            skill_id=skill.ref.skill_id,
            skill_version_id=skill.ref.skill_version_id,
            semantic_version="1.0.0",
            artifact=artifact,
            capabilities=("WORLD_READ", "WATER"),
            certified_at=context.requested_at,
            revoked_at=None,
        )
        active = ActiveSkill(certified, 1, context.requested_at)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(tenant_id,task_id,actor_id,content_hash,snapshot_json)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    task.task_id,
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
                ) VALUES (%s,%s,%s,%s,%s,5,0,%s,'farm-rules-1',%s,%s)
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

    def _headers(
        self,
        suffix: str,
        *,
        token: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}" if token is None else token,
            "X-Request-Id": f"req_http_gate_{suffix}",
            "X-Trace-Id": f"trace_http_gate_{suffix}",
            "X-Correlation-Id": f"corr_http_gate_{suffix}",
            "X-Schema-Version": "1.0.0",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        status, response_headers, raw = self._request_raw(
            method,
            target,
            headers=headers,
            body=body,
        )
        if not raw:
            raise AssertionError(
                f"HTTP {method} {target} returned {status} with an empty body: {response_headers}"
            )
        parsed = json.loads(raw.decode("utf-8"))
        self.assertIsInstance(parsed, dict)
        return status, response_headers, cast(dict[str, object], parsed)

    def _request_raw(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, raw
        finally:
            connection.close()

    def _assert_attempt_headers(
        self,
        response_headers: dict[str, str],
        request_headers: dict[str, str],
    ) -> None:
        self.assertEqual(response_headers["x-request-id"], request_headers["X-Request-Id"])
        self.assertEqual(response_headers["x-trace-id"], request_headers["X-Trace-Id"])
        self.assertEqual(
            response_headers["x-correlation-id"],
            request_headers["X-Correlation-Id"],
        )
        self.assertEqual(response_headers["cache-control"], "no-store")
        self.assertTrue(response_headers["content-type"].startswith("application/json"))

    def _assert_error(
        self,
        status: int,
        response_headers: dict[str, str],
        payload: dict[str, object],
        request_headers: dict[str, str],
        *,
        expected_status: int,
        expected_code: str,
    ) -> None:
        self.assertEqual(status, expected_status)
        self._assert_attempt_headers(response_headers, request_headers)
        self.validator.validate("schemas/common/error-response.schema.json", payload)
        error = cast(dict[str, object], payload["error"])
        self.assertEqual(error["code"], expected_code)
        status_schema = {
            "$ref": (
                "https://contracts.yaya.local/common/"
                f"error-responses-by-status.schema.json#/$defs/status{expected_status}"
            )
        }
        schema_validator = Draft202012Validator(
            status_schema,
            registry=self.validator._registry,  # pyright: ignore[reportPrivateUsage]
            format_checker=FormatChecker(),
        )
        schema_validator.validate(payload)

    def _accept(
        self,
        *,
        suffix: str,
        key: str,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object], dict[str, str]]:
        body = _raw(_body()) if raw_body is None else raw_body
        headers = self._headers(suffix, idempotency_key=key)
        headers["Content-Type"] = "application/json"
        status, response_headers, payload = self._request(
            "POST",
            f"/v1/agent-sessions/{SESSION_ID}/turns",
            headers=headers,
            body=body,
        )
        return status, response_headers, payload, headers

    async def _persist_failed_run(self) -> tuple[str, str]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT event_json,operation_context_json
                FROM yaya_command_jobs LIMIT 1
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("accepted HTTP command did not create a durable job")
        event = decode_as(row["event_json"], GameEvent)
        context = decode_as(row["operation_context_json"], OperationContext)
        if event.skill_ref is None:
            raise AssertionError("accepted skill event omitted its binding")
        invocation_id = "invocation_http_gate_0001"
        arguments = {"length": 8}
        request_hash = skill_invocation_request_sha256(
            tenant_id=context.actor.tenant_id,
            invocation_id=invocation_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            expected_world_revision=event.expected_world_revision,
            skill_ref=event.skill_ref,
            arguments=arguments,
        )
        request = SkillInvocationRequest(
            invocation_id=invocation_id,
            tenant_id=context.actor.tenant_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            command_id=event.command_id,
            world_id=WORLD_ID,
            expected_world_revision=event.expected_world_revision,
            skill_ref=event.skill_ref,
            arguments=arguments,
            request_sha256=request_hash,
        )
        world_engine = WateringWorldEngine()
        service = PostgresSkillInvocationService(
            database=self.database,
            sandbox=_TimedOutSandbox(),
            world_engine=world_engine,
            world_uow=PostgresWorldUnitOfWork(self.database, world_engine),
            limits=SandboxLimits(
                cpu_ms=1_000,
                wall_ms=3_000,
                memory_bytes=67_108_864,
                max_intents=8,
                max_output_bytes=65_536,
                max_processes=1,
                network_access=False,
            ),
            versions=make_versions(),
            contracts_root=CONTRACTS_ROOT,
        )
        result = await service.invoke(request, context)
        return result.run.run_id, result.run.evidence_refs[0].evidence_id

    async def _append_query_event(self, command_id: str) -> dict[str, object]:
        occurred_at = datetime.now(UTC)
        event: dict[str, object] = {
            "event_id": "evt_http_gate_00000001",
            "event_type": "world.http_gate_observed",
            "event_version": 1,
            "schema_version": "1.0.0",
            "stream_id": f"world:{WORLD_ID}",
            "sequence": 1,
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
            "producer": "agent_backend",
            "trace_id": "trace_http_gate_first0001",
            "command_id": command_id,
            "correlation_id": "corr_http_gate_first0001",
            "causation_id": command_id,
            "content_ref": plain(self.origin.content_ref),
            "payload": {"kind": "HTTP_GATE"},
        }
        self.validator.validate("schemas/common/event-envelope.schema.json", event)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_events(
                  tenant_id,event_id,stream_id,sequence,event_type,event_json,occurred_at
                ) VALUES (%s,%s,%s,1,%s,%s,%s)
                """,
                (
                    self.origin.actor.tenant_id,
                    event["event_id"],
                    event["stream_id"],
                    event["event_type"],
                    Jsonb(event),
                    occurred_at,
                ),
            )
            await connection.execute(
                """
                UPDATE yaya_worlds SET last_event_sequence=1
                WHERE tenant_id=%s AND world_id=%s
                """,
                (self.origin.actor.tenant_id, WORLD_ID),
            )
        finally:
            await connection.close()
        return event

    def test_loopback_accept_replay_and_all_read_routes(self) -> None:
        key = "agent-turn:http-gate:0001"
        first_status, first_headers, first_body, first_attempt = self._accept(
            suffix="first0001",
            key=key,
        )
        self.assertEqual(first_status, 202)
        self._assert_attempt_headers(first_headers, first_attempt)
        self.validator.validate("schemas/game/accepted-game-job.schema.json", first_body)
        self.assertEqual(first_headers["idempotency-replayed"], "false")
        self.assertEqual(first_headers["retry-after"], "1")
        command_id = cast(str, first_body["command_id"])
        self.assertEqual(first_headers["location"], f"/v1/commands/{command_id}")
        self.assertEqual(first_body["trace_id"], first_attempt["X-Trace-Id"])

        replay_status, replay_headers, replay_body, replay_attempt = self._accept(
            suffix="replay0002",
            key=key,
        )
        self.assertEqual(replay_status, 202)
        self._assert_attempt_headers(replay_headers, replay_attempt)
        self.assertEqual(replay_headers["idempotency-replayed"], "true")
        self.assertEqual(replay_headers["location"], first_headers["location"])
        self.assertEqual(replay_headers["retry-after"], "1")
        self.assertEqual(replay_body, first_body)
        self.assertEqual(replay_body["trace_id"], first_attempt["X-Trace-Id"])
        self.assertNotEqual(replay_headers["x-trace-id"], replay_body["trace_id"])

        run_id, evidence_id = asyncio.run(self._persist_failed_run())
        expected_event = asyncio.run(self._append_query_event(command_id))
        reads = [
            (
                f"/v1/commands/{command_id}",
                "schemas/game/command.schema.json",
                "command_id",
                command_id,
                None,
            ),
            (
                f"/v1/runs/{run_id}",
                "schemas/game/run.schema.json",
                "run_id",
                run_id,
                None,
            ),
            (
                f"/v1/worlds/{WORLD_ID}/snapshot",
                "schemas/game/world-snapshot.schema.json",
                "world_id",
                WORLD_ID,
                "x-world-revision",
            ),
            (
                f"/v1/evidence/{evidence_id}",
                "schemas/game/evidence.schema.json",
                None,
                evidence_id,
                "etag",
            ),
        ]
        for index, (target, schema, identity_field, identity, required_header) in enumerate(
            reads, start=1
        ):
            attempt = self._headers(f"read000{index}")
            status, response_headers, payload = self._request("GET", target, headers=attempt)
            self.assertEqual(status, 200, (target, payload))
            self._assert_attempt_headers(response_headers, attempt)
            self.validator.validate(schema, payload)
            if identity_field is None:
                reference = cast(dict[str, object], payload["evidence_ref"])
                self.assertEqual(reference["evidence_id"], identity)
            else:
                self.assertEqual(payload[identity_field], identity)
            if required_header is not None:
                self.assertIn(required_header, response_headers)

        event_attempt = self._headers("events005")
        status, response_headers, page = self._request(
            "GET",
            f"/v1/worlds/{WORLD_ID}/events?after_sequence=0&limit=100",
            headers=event_attempt,
        )
        self.assertEqual(status, 200)
        self._assert_attempt_headers(response_headers, event_attempt)
        self.validator.validate("schemas/game/world-event-page.schema.json", page)
        self.assertEqual(response_headers["x-world-revision"], "5")
        self.assertEqual(page["events"], [expected_event])
        self.assertEqual(page["next_after_sequence"], 1)

    def test_strict_json_oversize_invalid_token_and_cross_actor_are_contract_errors(self) -> None:
        duplicate = b'{"turn_id":"turn_http_gate_0001","turn_id":"turn_http_gate_0002"}'
        status, headers, payload, attempt = self._accept(
            suffix="duplicate001",
            key="agent-turn:http-gate:duplicate",
            raw_body=duplicate,
        )
        self._assert_error(
            status,
            headers,
            payload,
            attempt,
            expected_status=400,
            expected_code="INVALID_REQUEST",
        )

        oversize_attempt = self._headers(
            "oversize0001",
            idempotency_key="agent-turn:http-gate:oversize",
        )
        oversize_attempt["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("POST", f"/v1/agent-sessions/{SESSION_ID}/turns")
            for key, value in oversize_attempt.items():
                connection.putheader(key, value)
            connection.putheader("Content-Length", str(8 * 1024 * 1024 + 1))
            connection.endheaders()
            response = connection.getresponse()
            response_body = cast(dict[str, object], json.loads(response.read().decode("utf-8")))
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            self._assert_error(
                response.status,
                response_headers,
                response_body,
                oversize_attempt,
                expected_status=413,
                expected_code="PAYLOAD_TOO_LARGE",
            )
        finally:
            connection.close()

        invalid_token_attempt = self._headers(
            "badtoken0001",
            token="Bearer invalid.jwt.value",
        )
        status, headers, payload = self._request(
            "GET",
            "/v1/commands/cmd_http_gate_missing0001",
            headers=invalid_token_attempt,
        )
        self._assert_error(
            status,
            headers,
            payload,
            invalid_token_attempt,
            expected_status=401,
            expected_code="AUTHENTICATION_REQUIRED",
        )

        first_status, _, first_body, _ = self._accept(
            suffix="owner000001",
            key="agent-turn:http-gate:owner",
        )
        self.assertEqual(first_status, 202)
        command_id = cast(str, first_body["command_id"])
        other_actor = replace(self.origin.actor, actor_id="learner_http_other_0001")
        other_token = self.authenticator.issue_for_test(
            other_actor,
            now=datetime.now(UTC),
        )
        cross_attempt = self._headers(
            "crossactor01",
            token=f"Bearer {other_token}",
        )
        status, headers, payload = self._request(
            "GET", f"/v1/commands/{command_id}", headers=cross_attempt
        )
        self._assert_error(
            status,
            headers,
            payload,
            cross_attempt,
            expected_status=404,
            expected_code="NOT_FOUND",
        )

    def test_required_attempt_headers_and_idempotency_header_fail_closed(self) -> None:
        body = _raw(_body())
        complete = self._headers(
            "allheaders01",
            idempotency_key="agent-turn:http-gate:all-headers",
        )
        complete["Content-Type"] = "application/json"
        self.assertEqual(
            {
                "Authorization",
                "X-Request-Id",
                "X-Trace-Id",
                "X-Correlation-Id",
                "X-Schema-Version",
                "Idempotency-Key",
            },
            set(complete) - {"Content-Type"},
        )
        cases = (
            ("X-Request-Id", 400, "INVALID_REQUEST"),
            ("X-Trace-Id", 400, "INVALID_REQUEST"),
            ("X-Correlation-Id", 400, "INVALID_REQUEST"),
            ("X-Schema-Version", 409, "SCHEMA_VERSION_UNSUPPORTED"),
            ("Authorization", 401, "AUTHENTICATION_REQUIRED"),
            ("Idempotency-Key", 400, "INVALID_REQUEST"),
        )
        for index, (missing, expected_status, expected_code) in enumerate(cases):
            with self.subTest(index=index, missing=missing):
                attempt = dict(complete)
                del attempt[missing]
                if missing in {"X-Request-Id", "X-Trace-Id", "X-Correlation-Id"}:
                    status, response_headers, raw = self._request_raw(
                        "POST",
                        f"/v1/agent-sessions/{SESSION_ID}/turns",
                        headers=attempt,
                        body=body,
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(raw, b"")
                    self.assertEqual(response_headers["connection"].lower(), "close")
                    self.assertEqual(response_headers["content-length"], "0")
                    self.assertNotIn("x-request-id", response_headers)
                    self.assertNotIn("x-trace-id", response_headers)
                    self.assertNotIn("x-correlation-id", response_headers)
                    continue
                status, response_headers, payload = self._request(
                    "POST",
                    f"/v1/agent-sessions/{SESSION_ID}/turns",
                    headers=attempt,
                    body=body,
                )
                self._assert_attempt_headers(response_headers, attempt)
                self.validator.validate("schemas/common/error-response.schema.json", payload)
                self.assertEqual(status, expected_status)
                self.assertEqual(cast(dict[str, object], payload["error"])["code"], expected_code)


if __name__ == "__main__":
    unittest.main()
