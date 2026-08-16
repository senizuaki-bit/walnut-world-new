from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
import unittest
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_backend.application import HttpAttempt  # noqa: E402
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.database import (  # noqa: E402
    PostgresCommitStateUnknown,
    PostgresDatabase,
)
from yaya_agent_backend.product_application import ProductApplicationError  # noqa: E402
from yaya_agent_backend.product_http_api import ProductHttpApi  # noqa: E402
from yaya_agent_backend.skill_drafts import (  # noqa: E402
    PostgresSkillDraftRepository,
    ProductSkillDraftApplication,
)
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    canonical_json_sha256,
)

from tests.postgres_test_support import postgres_test_server  # noqa: E402

_SESSION_ID = "session_agent_001"
_DRAFT_ID = "draft_water_001"
_TASK_ID = "task_water_001"
_AUTHORITY_ID = "launch_auth_001"
_TARGET = "/product-experience/v1/sessions/session_agent_001/skill-drafts/draft_water_001"
_JWT_SECRET = "product-draft-test-secret-" + "s" * 48
_JWT_ISSUER = "yaya-product-draft-test"
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


def _create_body() -> dict[str, object]:
    body = copy.deepcopy(_example("product-skill-draft-upsert-request"))
    body["base_revision"] = 0
    body["base_draft_sha256"] = None
    return body


def _raw(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _payload(raw: bytes) -> dict[str, object]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("HTTP response is not a JSON object")
    return cast(dict[str, object], value)


class _CommitUnknownDatabase(PostgresDatabase):
    @asynccontextmanager
    async def transaction_with_commit_boundary(
        self,
    ) -> AsyncGenerator[Any]:
        async with super().transaction_with_commit_boundary() as connection:
            yield connection
        raise PostgresCommitStateUnknown("injected lost COMMIT acknowledgement")


class ProductSkillDraftPostgresTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._server_context = postgres_test_server()
        try:
            cls.server = cls._server_context.__enter__()
            cls.database = PostgresDatabase(cls.server.dsn)
            asyncio.run(cls.database.migrate())
        except BaseException:
            cls._server_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)

    async def asyncSetUp(self) -> None:
        self.actor = _actor()
        self.validator = ContractSchemaValidator(CONTRACTS_ROOT)
        self.repository = PostgresSkillDraftRepository(self.database, self.validator)
        self.application = ProductSkillDraftApplication(self.repository, self.validator)
        self.authenticator = JwtAuthenticator(
            hmac_secret=_JWT_SECRET,
            issuer=_JWT_ISSUER,
            audience=_JWT_AUDIENCE,
        )
        self.token = self.authenticator.issue_for_test(self.actor, now=datetime.now(UTC))
        self.api = ProductHttpApi(
            application=cast(Any, self.application),
            draft_application=self.application,
            authenticator=self.authenticator,
            validator=self.validator,
        )
        await self._reset_and_seed()

    async def _reset_and_seed(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                TRUNCATE yaya_product_write_receipts,yaya_skill_draft_heads,
                  yaya_skill_draft_revisions,yaya_public_agent_sessions,
                  yaya_launch_authorities,yaya_agent_profiles,yaya_learners,
                  yaya_worlds,yaya_tasks CASCADE
                """
            )
            session = copy.deepcopy(_example("game-agent-session"))
            context = cast(dict[str, object], session["request_context"])
            content = cast(dict[str, object], session["content"])
            world_id = cast(str, session["world_id"])
            learner_id = cast(str, session["learner_id"])
            profile_id = cast(str, session["agent_profile_id"])
            content_hash = cast(str, content["content_hash"])
            task = {"task_id": _TASK_ID, "content_ref": content}
            learner = {"learner_id": learner_id, "actor_id": self.actor.actor_id}
            profile = {"agent_profile_id": profile_id, "actor_id": self.actor.actor_id}
            authority = {
                "authority_id": _AUTHORITY_ID,
                "task_id": _TASK_ID,
                "world_id": world_id,
                "learner_id": learner_id,
                "agent_profile_id": profile_id,
                "content_ref": content,
            }
            await connection.execute(
                """
                INSERT INTO yaya_tasks(
                    tenant_id,task_id,actor_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    self.actor.tenant_id,
                    _TASK_ID,
                    self.actor.actor_id,
                    content_hash,
                    Jsonb(task),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                    tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                    last_event_sequence,state_hash,world_rules_version,state_json,
                    request_context_json
                ) VALUES (%s,%s,%s,%s,%s,0,0,%s,'farm-rules-12',%s,%s)
                """,
                (
                    self.actor.tenant_id,
                    world_id,
                    self.actor.actor_id,
                    content_hash,
                    f"world:{world_id}",
                    canonical_json_sha256({}),
                    Jsonb({}),
                    Jsonb(context),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_learners(
                    tenant_id,learner_id,actor_id,content_hash,record_sha256,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    self.actor.tenant_id,
                    learner_id,
                    self.actor.actor_id,
                    content_hash,
                    canonical_json_sha256(learner),
                    Jsonb(learner),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_agent_profiles(
                    tenant_id,agent_profile_id,actor_id,content_hash,
                    record_sha256,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    self.actor.tenant_id,
                    profile_id,
                    self.actor.actor_id,
                    content_hash,
                    canonical_json_sha256(profile),
                    Jsonb(profile),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_launch_authorities(
                    tenant_id,authority_id,actor_id,learner_id,content_unit_id,
                    content_version,content_hash,world_id,agent_profile_id,task_id,
                    versions_json,snapshot_sha256
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    self.actor.tenant_id,
                    _AUTHORITY_ID,
                    self.actor.actor_id,
                    learner_id,
                    content["unit_id"],
                    content["version"],
                    content_hash,
                    world_id,
                    profile_id,
                    _TASK_ID,
                    Jsonb(session["versions"]),
                    canonical_json_sha256(authority),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_public_agent_sessions(
                    tenant_id,session_id,authority_id,actor_id,content_hash,task_id,
                    world_id,learner_id,agent_profile_id,status,resource_sha256,
                    resource_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    self.actor.tenant_id,
                    _SESSION_ID,
                    _AUTHORITY_ID,
                    self.actor.actor_id,
                    content_hash,
                    _TASK_ID,
                    world_id,
                    learner_id,
                    profile_id,
                    session["status"],
                    canonical_json_sha256(session),
                    Jsonb(session),
                ),
            )
        finally:
            await connection.close()

    def _headers(self, suffix: str, *, key: str = "draft-save-key-00000001") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_draft_{suffix}",
            "X-Trace-Id": f"trace_draft_{suffix}",
            "X-Correlation-Id": f"corr_draft_{suffix}",
            "Idempotency-Key": key,
            "Content-Type": "application/json",
        }

    def _attempt(self, suffix: str) -> HttpAttempt:
        return HttpAttempt(
            request_id=f"req_draft_{suffix}",
            trace_id=f"trace_draft_{suffix}",
            correlation_id=f"corr_draft_{suffix}",
            requested_at=datetime.now(UTC),
        )

    @staticmethod
    def _error_code(response_body: bytes) -> str:
        payload = _payload(response_body)
        error = cast(dict[str, object], payload["error"])
        return cast(str, error["code"])

    async def _create(self) -> tuple[dict[str, object], bytes]:
        body = _create_body()
        raw = _raw(body)
        result = await self.api.handle("PUT", _TARGET, self._headers("create0001"), raw)
        self.assertEqual(result.status, 201, _payload(result.body))
        self.assertEqual(result.headers["Idempotency-Replayed"], "false")
        return _payload(result.body), raw

    async def test_http_create_update_get_and_historic_exact_replay(self) -> None:
        created, create_raw = await self._create()
        self.validator.validate("schemas/product-experience/skill-draft.schema.json", created)
        self.assertEqual(created["revision"], 1)
        self.assertEqual(
            created["draft_sha256"],
            canonical_json_sha256(
                {
                    key: created[key]
                    for key in (
                        "session_id",
                        "draft_id",
                        "skill_id",
                        "content_ref",
                        "display_name",
                        "source_bundle",
                    )
                }
            ),
        )
        replay = await self.api.handle(
            "PUT",
            _TARGET,
            self._headers("replay001"),
            create_raw,
        )
        self.assertEqual((replay.status, replay.body), (201, _raw(created)))
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.headers["Location"], _TARGET)

        update = _create_body()
        update["base_revision"] = 1
        update["base_draft_sha256"] = created["draft_sha256"]
        update["display_name"] = "Water twice"
        update_raw = _raw(update)
        updated_response = await self.api.handle(
            "PUT",
            _TARGET,
            self._headers("update001", key="draft-save-key-00000002"),
            update_raw,
        )
        self.assertEqual(updated_response.status, 200)
        updated = _payload(updated_response.body)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["request_context"], created["request_context"])
        self.assertEqual(updated["created_at"], created["created_at"])

        get_response = await self.api.handle("GET", _TARGET, self._headers("get000001"))
        self.assertEqual((get_response.status, _payload(get_response.body)), (200, updated))
        self.assertEqual(get_response.headers["X-Draft-Revision"], "2")
        self.assertNotIn("Location", get_response.headers)
        self.assertNotIn("Idempotency-Replayed", get_response.headers)

        old_replay = await self.api.handle(
            "PUT",
            _TARGET,
            self._headers("replay002"),
            create_raw,
        )
        self.assertEqual((old_replay.status, _payload(old_replay.body)), (201, created))
        connection = await self.database.connect(autocommit=True)
        try:
            counts = await (
                await connection.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM yaya_skill_draft_revisions) AS revisions,
                      (SELECT COUNT(*) FROM yaya_skill_draft_heads) AS heads,
                      (SELECT COUNT(*) FROM yaya_product_write_receipts) AS receipts
                    """
                )
            ).fetchone()
            self.assertEqual(counts, {"revisions": 2, "heads": 1, "receipts": 2})
        finally:
            await connection.close()

    async def test_raw_byte_idempotency_and_exact_cas_concurrency(self) -> None:
        created, create_raw = await self._create()
        whitespace_replay = create_raw.replace(b'"session_id"', b'  "session_id"', 1)
        reused = await self.api.handle(
            "PUT",
            _TARGET,
            self._headers("reuse0001"),
            whitespace_replay,
        )
        self.assertEqual(
            (reused.status, self._error_code(reused.body)), (409, "IDEMPOTENCY_KEY_REUSED")
        )

        first = _create_body()
        first["base_revision"] = 1
        first["base_draft_sha256"] = created["draft_sha256"]
        first["display_name"] = "Concurrent A"
        second = copy.deepcopy(first)
        second["display_name"] = "Concurrent B"

        async def update(
            value: dict[str, object],
            suffix: str,
            key: str,
        ) -> object:
            try:
                return await self.application.upsert_skill_draft(
                    self.actor,
                    self._attempt(suffix),
                    _SESSION_ID,
                    _DRAFT_ID,
                    key,
                    _raw(value),
                    value,
                )
            except ProductApplicationError as error:
                return error

        outcomes = await asyncio.gather(
            update(first, "race0001", "draft-race-key-00000001"),
            update(second, "race0002", "draft-race-key-00000002"),
        )
        successes = [value for value in outcomes if not isinstance(value, BaseException)]
        failures = [value for value in outcomes if isinstance(value, ProductApplicationError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "CONTENT_VERSION_MISMATCH")
        current = await self.application.get_skill_draft(self.actor, _SESSION_ID, _DRAFT_ID)
        self.assertEqual(current.payload["revision"], 2)

    async def test_strict_json_source_semantics_path_closure_and_413(self) -> None:
        base = _create_body()
        cases: list[tuple[str, bytes, int, str]] = []
        duplicate = _raw(base)[:-1] + b',"session_id":"session_agent_001"}'
        cases.append(("duplicate", duplicate, 400, "INVALID_REQUEST"))
        cases.append(("utf8", b'{"invalid":"\xff"}', 400, "INVALID_REQUEST"))
        unknown = copy.deepcopy(base)
        unknown["unknown"] = True
        cases.append(("unknown", _raw(unknown), 400, "INVALID_REQUEST"))
        surrogate = copy.deepcopy(base)
        surrogate["display_name"] = "\ud800"
        surrogate_raw = json.dumps(
            surrogate,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        cases.append(("surrogate", surrogate_raw, 400, "INVALID_REQUEST"))
        bad_hash = copy.deepcopy(base)
        files = cast(
            list[dict[str, object]], cast(dict[str, object], bad_hash["source_bundle"])["files"]
        )
        files[0]["content_sha256"] = "0" * 64
        cases.append(("filehash", _raw(bad_hash), 400, "INVALID_REQUEST"))
        collision = copy.deepcopy(base)
        bundle = cast(dict[str, object], collision["source_bundle"])
        original = copy.deepcopy(cast(list[dict[str, object]], bundle["files"])[0])
        original["path"] = "src/Main.cpp"
        original["content"] = "int other() { return 1; }"
        original["content_sha256"] = hashlib.sha256(
            cast(str, original["content"]).encode()
        ).hexdigest()
        cast(list[dict[str, object]], bundle["files"]).append(original)
        cases.append(("fold", _raw(collision), 400, "INVALID_REQUEST"))
        total = copy.deepcopy(base)
        total_bundle = cast(dict[str, object], total["source_bundle"])
        total_files = cast(list[dict[str, object]], total_bundle["files"])
        total_files.clear()
        for index in range(2):
            content = "x" * 600_000
            total_files.append(
                {
                    "path": f"src/file{index}.cpp",
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
        total_bundle["entrypoint"] = "src/file0.cpp"
        cases.append(("total", _raw(total), 400, "INVALID_REQUEST"))
        for index, (name, raw, status, code) in enumerate(cases):
            with self.subTest(name=name):
                response = await self.api.handle(
                    "PUT",
                    _TARGET,
                    self._headers(f"invalid{index:02d}"),
                    raw,
                )
                self.assertEqual((response.status, self._error_code(response.body)), (status, code))

        wrong_path = _TARGET.replace(_DRAFT_ID, "draft_other_001")
        mismatch = await self.api.handle(
            "PUT",
            wrong_path,
            self._headers("path00001"),
            _raw(base),
        )
        self.assertEqual(
            (mismatch.status, self._error_code(mismatch.body)),
            (409, "CONTENT_VERSION_MISMATCH"),
        )
        oversized_headers = self._headers("large0001")
        oversized_headers["X-YaYa-Transport-Invalid"] = "PAYLOAD_TOO_LARGE"
        oversized = await self.api.handle("PUT", _TARGET, oversized_headers)
        self.assertEqual(
            (oversized.status, self._error_code(oversized.body)),
            (413, "PAYLOAD_TOO_LARGE"),
        )

    async def test_get_is_zero_write_and_corruption_fails_closed(self) -> None:
        await self._create()

        async def anchors() -> dict[str, object]:
            connection = await self.database.connect(autocommit=True)
            try:
                row = await (
                    await connection.execute(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM yaya_skill_draft_revisions) AS revisions,
                          (SELECT COUNT(*) FROM yaya_product_write_receipts) AS receipts,
                          (SELECT current_revision FROM yaya_skill_draft_heads) AS head_revision,
                          (SELECT updated_at FROM yaya_skill_draft_heads) AS head_updated
                        """
                    )
                ).fetchone()
                if row is None:
                    raise AssertionError("anchor query returned no row")
                return row
            finally:
                await connection.close()

        before = await anchors()
        for index in range(3):
            result = await self.application.get_skill_draft(
                self.actor,
                _SESSION_ID,
                _DRAFT_ID,
            )
            self.assertEqual(result.payload["revision"], 1, index)
        self.assertEqual(await anchors(), before)

        connection = await self.database.connect(autocommit=True)
        try:
            with self.assertRaises(psycopg.Error):
                await connection.execute(
                    "UPDATE yaya_skill_draft_revisions SET resource_sha256=%s",
                    ("0" * 64,),
                )
            await connection.execute(
                "UPDATE yaya_skill_draft_heads SET updated_at=updated_at + interval '1 second'"
            )
        finally:
            await connection.close()
        with self.assertRaises(ProductApplicationError) as raised:
            await self.application.get_skill_draft(self.actor, _SESSION_ID, _DRAFT_ID)
        self.assertEqual(
            (raised.exception.http_status, raised.exception.code), (500, "INVARIANT_VIOLATION")
        )

    async def test_session_actor_and_content_authority_are_closed(self) -> None:
        wrong_content = _create_body()
        content = cast(dict[str, object], wrong_content["content_ref"])
        content["content_hash"] = "b" * 64
        response = await self.api.handle(
            "PUT",
            _TARGET,
            self._headers("content01"),
            _raw(wrong_content),
        )
        self.assertEqual(
            (response.status, self._error_code(response.body)),
            (409, "CONTENT_VERSION_MISMATCH"),
        )
        await self._create()

        other_actor = ActorRef(
            tenant_id=self.actor.tenant_id,
            actor_id="student_other_001",
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        )
        with self.assertRaises(ProductApplicationError) as hidden:
            await self.application.get_skill_draft(other_actor, _SESSION_ID, _DRAFT_ID)
        self.assertEqual((hidden.exception.http_status, hidden.exception.code), (404, "NOT_FOUND"))

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_public_agent_sessions
                SET resource_json=jsonb_set(
                    resource_json,'{content,content_hash}',to_jsonb(%s::text)
                )
                WHERE tenant_id=%s AND session_id=%s
                """,
                ("b" * 64, self.actor.tenant_id, _SESSION_ID),
            )
        finally:
            await connection.close()
        with self.assertRaises(ProductApplicationError) as corrupt:
            await self.application.get_skill_draft(self.actor, _SESSION_ID, _DRAFT_ID)
        self.assertEqual(
            (corrupt.exception.http_status, corrupt.exception.code),
            (500, "INVARIANT_VIOLATION"),
        )

    async def test_lost_commit_ack_returns_reconciliation_then_exact_replay(self) -> None:
        unknown_database = _CommitUnknownDatabase(self.server.dsn)
        unknown_repository = PostgresSkillDraftRepository(unknown_database, self.validator)
        unknown_application = ProductSkillDraftApplication(unknown_repository, self.validator)
        unknown_api = ProductHttpApi(
            application=cast(Any, unknown_application),
            draft_application=unknown_application,
            authenticator=self.authenticator,
            validator=self.validator,
        )
        body = _create_body()
        raw = _raw(body)
        response = await unknown_api.handle(
            "PUT",
            _TARGET,
            self._headers("unknown01"),
            raw,
        )
        self.assertEqual(response.status, 503)
        reconciliation = _payload(response.body)
        self.validator.validate(
            "schemas/product-experience/product-write-reconciliation.schema.json",
            reconciliation,
        )
        self.assertEqual(response.headers["Location"], _TARGET)
        self.assertNotIn("Retry-After", response.headers)

        get_result = await self.api.handle("GET", _TARGET, self._headers("recover01"))
        self.assertEqual(get_result.status, 200)
        replay = await self.api.handle(
            "PUT",
            _TARGET,
            self._headers("recover02"),
            raw,
        )
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(_payload(replay.body), _payload(get_result.body))


if __name__ == "__main__":
    unittest.main()
