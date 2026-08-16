from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import copy
import hashlib
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from a8_state_fingerprint import (  # noqa: E402
    A8_DRAFT_MUTATION_TABLES,
    A8StateFingerprint,
    a8_state_fingerprint,
    fingerprint_without,
    missing_a8_business_tables,
)
from yaya_agent_backend.skill_drafts import (  # noqa: E402
    PostgresSkillDraftRepository,
    ProductSkillDraftApplication,
)
from yaya_agent_contracts import ActorRef, ActorType  # noqa: E402

from tests import test_agent_backend_skill_drafts as draft_tests  # noqa: E402


def _source_file(path: str, content: str) -> dict[str, object]:
    return {
        "path": path,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _bundle(body: Mapping[str, object]) -> dict[str, object]:
    return cast(dict[str, object], body["source_bundle"])


def _files(body: Mapping[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], _bundle(body)["files"])


def _error_code(payload: Mapping[str, object]) -> str:
    return cast(str, cast(Mapping[str, object], payload["error"])["code"])


class ProductSkillDraftFailureMatrixTests(draft_tests.ProductSkillDraftPostgresTests):
    """Frozen Product HTTP semantics over the real PostgreSQL Draft authority."""

    # The original focused regression class remains independently discoverable.
    test_http_create_update_get_and_historic_exact_replay = None  # type: ignore[assignment]
    test_raw_byte_idempotency_and_exact_cas_concurrency = None  # type: ignore[assignment]
    test_strict_json_source_semantics_path_closure_and_413 = None  # type: ignore[assignment]
    test_get_is_zero_write_and_corruption_fails_closed = None  # type: ignore[assignment]
    test_session_actor_and_content_authority_are_closed = None  # type: ignore[assignment]
    test_lost_commit_ack_returns_reconciliation_then_exact_replay = None  # type: ignore[assignment]

    async def _state(self) -> A8StateFingerprint:
        fingerprint = await a8_state_fingerprint(self.database)
        self.assertEqual(missing_a8_business_tables(fingerprint), ())
        return fingerprint

    async def _put(
        self,
        body: Mapping[str, object],
        *,
        suffix: str,
        key: str,
        target: str = draft_tests._TARGET,
        raw_body: bytes | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object], bytes]:
        raw = draft_tests._raw(body) if raw_body is None else raw_body
        headers = self._headers(suffix, key=key)
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        response = await self.api.handle("PUT", target, headers, raw)
        return response.status, dict(response.headers), draft_tests._payload(response.body), raw

    async def _assert_rejected_without_write(
        self,
        body: Mapping[str, object],
        *,
        suffix: str,
        key: str,
        status: int,
        code: str,
        target: str = draft_tests._TARGET,
        raw_body: bytes | None = None,
        token: str | None = None,
    ) -> None:
        before = await self._state()
        actual_status, _, payload, _ = await self._put(
            body,
            suffix=suffix,
            key=key,
            target=target,
            raw_body=raw_body,
            token=token,
        )
        self.assertEqual((actual_status, _error_code(payload)), (status, code), payload)
        self.assertEqual(await self._state(), before)

    @staticmethod
    def _update_body(created: Mapping[str, object]) -> dict[str, object]:
        body = draft_tests._create_body()
        body["base_revision"] = created["revision"]
        body["base_draft_sha256"] = created["draft_sha256"]
        return body

    async def test_isolated_cas_and_immutable_identity_drift_write_nothing(self) -> None:
        created, _ = await self._create()
        current_hash = cast(str, created["draft_sha256"])

        cases: list[tuple[str, dict[str, object], int]] = []
        stale_revision = self._update_body(created)
        stale_revision["base_revision"] = 2
        cases.append(("stale_revision", stale_revision, 409))

        stale_hash = self._update_body(created)
        stale_hash["base_draft_sha256"] = "f" * 64
        cases.append(("stale_hash", stale_hash, 409))

        inconsistent_pair = self._update_body(created)
        inconsistent_pair["base_revision"] = 0
        inconsistent_pair["base_draft_sha256"] = current_hash
        cases.append(("inconsistent_pair", inconsistent_pair, 400))

        wrong_content = self._update_body(created)
        content_ref = cast(dict[str, object], wrong_content["content_ref"])
        content_ref["content_hash"] = "b" * 64
        cases.append(("content_drift", wrong_content, 409))

        wrong_skill = self._update_body(created)
        wrong_skill["skill_id"] = "skill_other_001"
        cases.append(("skill_drift", wrong_skill, 409))

        for index, (label, body, expected_status) in enumerate(cases, start=1):
            with self.subTest(label=label):
                await self._assert_rejected_without_write(
                    body,
                    suffix=f"cas_{index:04d}",
                    key=f"draft-matrix-cas-{index:04d}",
                    status=expected_status,
                    code=(
                        "INVALID_REQUEST" if expected_status == 400 else "CONTENT_VERSION_MISMATCH"
                    ),
                )

        update = self._update_body(created)
        other_actor = ActorRef(
            tenant_id=self.actor.tenant_id,
            actor_id="student_other_001",
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        )
        other_token = self.authenticator.issue_for_test(other_actor, now=datetime.now(UTC))
        await self._assert_rejected_without_write(
            update,
            suffix="actor_drift_0001",
            key="draft-matrix-actor-drift-0001",
            status=404,
            code="NOT_FOUND",
            token=other_token,
        )

    async def test_path_body_session_and_draft_mismatch_write_nothing(self) -> None:
        body = draft_tests._create_body()
        targets = (
            (
                "session",
                "/product-experience/v1/sessions/session_other_001/skill-drafts/draft_water_001",
            ),
            (
                "draft",
                "/product-experience/v1/sessions/session_agent_001/skill-drafts/draft_other_001",
            ),
        )
        for index, (label, target) in enumerate(targets, start=1):
            with self.subTest(label=label):
                await self._assert_rejected_without_write(
                    body,
                    suffix=f"path_{index:04d}",
                    key=f"draft-matrix-path-{index:04d}",
                    target=target,
                    status=409,
                    code="CONTENT_VERSION_MISMATCH",
                )

    async def test_source_and_strict_json_failures_write_nothing(self) -> None:
        cases: list[tuple[str, dict[str, object], bytes | None]] = []

        unknown = draft_tests._create_body()
        unknown["unknown"] = True
        cases.append(("unknown_field", unknown, None))

        duplicate_path = draft_tests._create_body()
        _files(duplicate_path).extend(
            [
                _source_file("src/helper.cpp", "int helper() { return 1; }\n"),
                _source_file("src/helper.cpp", "int helper() { return 2; }\n"),
            ]
        )
        cases.append(("duplicate_path", duplicate_path, None))

        folded_path = draft_tests._create_body()
        _files(folded_path).append(_source_file("src/Main.cpp", "int alternate() { return 1; }\n"))
        cases.append(("ascii_fold_collision", folded_path, None))

        missing_entrypoint = draft_tests._create_body()
        _bundle(missing_entrypoint)["entrypoint"] = "src/missing.cpp"
        cases.append(("missing_entrypoint", missing_entrypoint, None))

        duplicate_entrypoint = draft_tests._create_body()
        _files(duplicate_entrypoint).append(copy.deepcopy(_files(duplicate_entrypoint)[0]))
        cases.append(("duplicate_entrypoint", duplicate_entrypoint, None))

        wrong_hash = draft_tests._create_body()
        _files(wrong_hash)[0]["content_sha256"] = "0" * 64
        cases.append(("wrong_file_hash", wrong_hash, None))

        absolute_path = draft_tests._create_body()
        _bundle(absolute_path)["entrypoint"] = "/src/main.cpp"
        _files(absolute_path)[0]["path"] = "/src/main.cpp"
        cases.append(("absolute_path", absolute_path, None))

        parent_escape = draft_tests._create_body()
        _bundle(parent_escape)["entrypoint"] = "src/../main.cpp"
        _files(parent_escape)[0]["path"] = "src/../main.cpp"
        cases.append(("parent_path_escape", parent_escape, None))

        file_count = draft_tests._create_body()
        count_files = [
            _source_file(f"src/file_{index:02d}.cpp", f"int value_{index} = {index};\n")
            for index in range(33)
        ]
        _bundle(file_count)["entrypoint"] = "src/file_00.cpp"
        _bundle(file_count)["files"] = count_files
        cases.append(("file_count", file_count, None))

        aggregate_bytes = draft_tests._create_body()
        first = "a" * 600_000
        second = "b" * 600_000
        _bundle(aggregate_bytes)["entrypoint"] = "src/main.cpp"
        _bundle(aggregate_bytes)["files"] = [
            _source_file("src/main.cpp", first),
            _source_file("src/helper.cpp", second),
        ]
        cases.append(("aggregate_bytes", aggregate_bytes, None))

        duplicate_key = draft_tests._create_body()
        duplicate_key_raw = (
            draft_tests._raw(duplicate_key)[:-1] + b',"session_id":"session_agent_001"}'
        )
        cases.append(("duplicate_json_key", duplicate_key, duplicate_key_raw))

        for index, (label, body, raw_body) in enumerate(cases, start=1):
            with self.subTest(label=label):
                await self._assert_rejected_without_write(
                    body,
                    raw_body=raw_body,
                    suffix=f"source_{index:04d}",
                    key=f"draft-matrix-source-{index:04d}",
                    status=400,
                    code="INVALID_REQUEST",
                )

    async def test_idempotency_conflict_and_concurrent_cas_have_exact_winners(self) -> None:
        created, original_raw = await self._create()
        changed_create = draft_tests._create_body()
        changed_create["display_name"] = "Reused with different bytes"
        await self._assert_rejected_without_write(
            changed_create,
            raw_body=draft_tests._raw(changed_create),
            suffix="idem_reuse_0001",
            key="draft-save-key-00000001",
            status=409,
            code="IDEMPOTENCY_KEY_REUSED",
        )
        self.assertNotEqual(draft_tests._raw(changed_create), original_raw)

        first = self._update_body(created)
        first["display_name"] = "Concurrent winner A"
        second = copy.deepcopy(first)
        second["display_name"] = "Concurrent winner B"
        before = await self._state()
        outcomes = await asyncio.gather(
            self._put(
                first,
                suffix="race_0001",
                key="draft-matrix-race-0001",
            ),
            self._put(
                second,
                suffix="race_0002",
                key="draft-matrix-race-0002",
            ),
        )
        statuses = sorted(item[0] for item in outcomes)
        self.assertEqual(statuses, [200, 409])
        rejected = next(item for item in outcomes if item[0] == 409)
        self.assertEqual(_error_code(rejected[2]), "CONTENT_VERSION_MISMATCH")
        after = await self._state()
        self.assertEqual(
            fingerprint_without(after, A8_DRAFT_MUTATION_TABLES),
            fingerprint_without(before, A8_DRAFT_MUTATION_TABLES),
        )
        self.assertEqual(
            after["yaya_skill_draft_revisions"].row_count,
            before["yaya_skill_draft_revisions"].row_count + 1,
        )
        self.assertEqual(
            after["yaya_skill_draft_heads"].row_count,
            before["yaya_skill_draft_heads"].row_count,
        )
        self.assertNotEqual(
            after["yaya_skill_draft_heads"].rows_md5,
            before["yaya_skill_draft_heads"].rows_md5,
        )
        self.assertEqual(
            after["yaya_product_write_receipts"].row_count,
            before["yaya_product_write_receipts"].row_count + 1,
        )
        current = await self.application.get_skill_draft(
            self.actor,
            draft_tests._SESSION_ID,
            draft_tests._DRAFT_ID,
        )
        self.assertEqual(current.payload["revision"], 2)

    async def test_commit_response_loss_reconciles_without_second_write(self) -> None:
        unknown_database = draft_tests._CommitUnknownDatabase(self.server.dsn)
        repository = PostgresSkillDraftRepository(unknown_database, self.validator)
        application = ProductSkillDraftApplication(repository, self.validator)
        api = draft_tests.ProductHttpApi(
            application=cast(Any, application),
            draft_application=application,
            authenticator=self.authenticator,
            validator=self.validator,
        )
        body = draft_tests._create_body()
        raw = draft_tests._raw(body)
        key = "draft-matrix-response-loss-0001"
        response = await api.handle(
            "PUT",
            draft_tests._TARGET,
            self._headers("response_loss_0001", key=key),
            raw,
        )
        payload = draft_tests._payload(response.body)
        self.assertEqual((response.status, _error_code(payload)), (503, "DEPENDENCY_UNAVAILABLE"))
        error = cast(dict[str, object], payload["error"])
        self.assertEqual(
            cast(dict[str, object], error["details"])["operation_was_durably_accepted"],
            True,
        )
        committed = await self._state()
        self.assertEqual(
            (
                committed["yaya_skill_draft_revisions"].row_count,
                committed["yaya_skill_draft_heads"].row_count,
                committed["yaya_product_write_receipts"].row_count,
            ),
            (1, 1, 1),
        )

        get_result = await self.api.handle(
            "GET",
            draft_tests._TARGET,
            self._headers("response_loss_get_0002"),
        )
        self.assertEqual(get_result.status, 200)
        replay = await self.api.handle(
            "PUT",
            draft_tests._TARGET,
            self._headers("response_loss_replay_0003", key=key),
            raw,
        )
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(await self._state(), committed)

    async def _force_corruption(
        self,
        statement: LiteralString,
        parameters: tuple[object, ...],
    ) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute("SET session_replication_role = replica")
            try:
                await connection.execute(statement, parameters)
            finally:
                await connection.execute("SET session_replication_role = origin")
        finally:
            await connection.close()

    async def test_receipt_revision_and_head_tamper_fail_closed_without_repair(self) -> None:
        cases = ("receipt", "revision", "head")
        for index, label in enumerate(cases, start=1):
            with self.subTest(label=label):
                await self._reset_and_seed()
                _, raw = await self._create()
                if label == "receipt":
                    await self._force_corruption(
                        "UPDATE yaya_product_write_receipts SET response_sha256=%s",
                        ("0" * 64,),
                    )
                elif label == "revision":
                    await self._force_corruption(
                        "UPDATE yaya_skill_draft_revisions SET resource_sha256=%s",
                        ("0" * 64,),
                    )
                else:
                    await self._force_corruption(
                        "UPDATE yaya_skill_draft_heads "
                        "SET updated_at=updated_at + interval '1 second'",
                        (),
                    )
                corrupted = await self._state()

                if label == "receipt":
                    response = await self.api.handle(
                        "PUT",
                        draft_tests._TARGET,
                        self._headers(
                            f"tamper_{index:04d}",
                            key="draft-save-key-00000001",
                        ),
                        raw,
                    )
                else:
                    response = await self.api.handle(
                        "GET",
                        draft_tests._TARGET,
                        self._headers(f"tamper_{index:04d}"),
                    )
                payload = draft_tests._payload(response.body)
                self.assertEqual(
                    (response.status, _error_code(payload)),
                    (500, "INVARIANT_VIOLATION"),
                    payload,
                )
                self.assertEqual(await self._state(), corrupted)


if __name__ == "__main__":
    import unittest

    unittest.main()
