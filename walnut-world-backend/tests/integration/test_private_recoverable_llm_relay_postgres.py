from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from yaya_agent_contracts import canonical_json_sha256

from walnut_backend.adapters.postgres.models import RecoverableLlmDispatchRow
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.llm_relay.protocol import (
    RelayDispatchConflict,
    RelayDispatchExpired,
    RelayProtocolError,
    RelayPutRequest,
    canonical_response_bytes,
    parse_put_request,
    resource_document,
)
from walnut_backend.llm_relay.store import (
    PostgresRelayStore,
    RelayGenerationLimitExceeded,
)
from walnut_backend.llm_relay.upstream import ProviderHttpResponse

PROVIDER = "postgres-relay-test"
MODEL = "postgres-relay-model"


def test_postgres_relay_is_atomic_restart_safe_and_never_reopens_generation() -> None:
    asyncio.run(_exercise_postgres_relay(_database_url()))


def test_postgres_global_generation_limit_refuses_thirteenth_before_claim() -> None:
    asyncio.run(_exercise_postgres_global_generation_limit(_database_url()))


async def _exercise_postgres_global_generation_limit(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    store = PostgresRelayStore(sessions, result_retention_seconds=604_800)
    requests = [_request(_dispatch_id(), content=f"budget-{index}") for index in range(13)]
    async with sessions() as session, session.begin():
        await session.execute(delete(RecoverableLlmDispatchRow))
    try:
        response = ProviderHttpResponse(200, "application/json", b'{"budget":"accepted"}')
        for index, request in enumerate(requests[:12]):
            inserted = await store.put(request, max_total_generations=12)
            assert inserted.created is True
            claim = await store.claim_next(2.0, max_total_generations=12)
            assert claim is not None
            assert claim.dispatch_id == request.dispatch_id
            assert claim.generation_count == 1
            completed = await store.complete_response(claim, response)
            assert completed.state == "SUCCEEDED", index

        thirteenth = requests[12]
        with pytest.raises(RelayGenerationLimitExceeded, match="before Provider POST"):
            await store.put(thirteenth, max_total_generations=12)
        assert await store.get(thirteenth.dispatch_id) is None
        async with sessions() as session:
            rows_after_refusal = list(
                await session.scalars(select(RecoverableLlmDispatchRow))
            )
        assert len(rows_after_refusal) == 12
        assert sum(row.generation_count for row in rows_after_refusal) == 12

        # Model a pending row written by an uncapped/older process. The final
        # capped claim boundary must still fail before changing its durable
        # generation fence, which is the last step before any upstream POST.
        assert (await store.put(thirteenth)).created is True
        with pytest.raises(RelayGenerationLimitExceeded, match="before Provider POST"):
            await store.claim_next(2.0, max_total_generations=12)
        async with sessions() as session:
            refused_row = await session.scalar(
                select(RecoverableLlmDispatchRow).where(
                    RecoverableLlmDispatchRow.dispatch_id == thirteenth.dispatch_id
                )
            )
        assert refused_row is not None
        assert refused_row.state == "PENDING"
        assert refused_row.generation_count == 0
        assert refused_row.dispatch_started_at is None
        assert refused_row.upstream_deadline_at is None
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(RecoverableLlmDispatchRow))
        await sessions.kw["bind"].dispose()


async def _exercise_postgres_relay(database_url: str) -> None:
    sessions = create_session_factory(database_url)
    store = PostgresRelayStore(sessions, result_retention_seconds=604_800)
    first = _request(_dispatch_id())
    unknown = _request(_dispatch_id())
    async with sessions() as session, session.begin():
        await session.execute(delete(RecoverableLlmDispatchRow))
    try:
        puts = await asyncio.gather(*(store.put(first) for _ in range(12)))
        assert sum(item.created for item in puts) == 1
        assert {item.resource.request_body_sha256 for item in puts} == {first.body_sha256}

        claims = await asyncio.gather(*(store.claim_next(2.0) for _ in range(8)))
        fenced = [claim for claim in claims if claim is not None]
        assert len(fenced) == 1
        claim = fenced[0]
        assert claim.dispatch_id == first.dispatch_id
        assert claim.generation_count == 1
        assert await store.claim_next(2.0) is None

        raw = b'{"id":"provider-once","choices":[],"usage":{}}'
        terminal = await store.complete_response(
            claim,
            ProviderHttpResponse(200, "application/json; charset=utf-8", raw),
        )
        assert terminal.state == "SUCCEEDED"
        restarted = PostgresRelayStore(sessions, result_retention_seconds=604_800)
        recovered = await restarted.get(first.dispatch_id)
        assert recovered is not None
        assert recovered.response_body == raw
        assert recovered.generation_count == 1
        assert (await restarted.put(first)).created is False

        changed = _request(first.dispatch_id, content="different immutable bytes")
        with pytest.raises(RelayDispatchConflict):
            await restarted.put(changed)

        async with sessions() as session, session.begin():
            await session.execute(
                update(RecoverableLlmDispatchRow)
                .where(RecoverableLlmDispatchRow.dispatch_id == first.dispatch_id)
                .values(provider="tampered-provider")
            )
        with pytest.raises(RelayDispatchConflict):
            await restarted.put(first)
        async with sessions() as session, session.begin():
            await session.execute(
                update(RecoverableLlmDispatchRow)
                .where(RecoverableLlmDispatchRow.dispatch_id == first.dispatch_id)
                .values(provider=PROVIDER, response_body=b"tampered-response")
            )
        corrupted = await restarted.get(first.dispatch_id)
        assert corrupted is not None
        with pytest.raises(RelayProtocolError, match="corrupt"):
            resource_document(corrupted, replayed=True)
        async with sessions() as session, session.begin():
            await session.execute(
                update(RecoverableLlmDispatchRow)
                .where(RecoverableLlmDispatchRow.dispatch_id == first.dispatch_id)
                .values(response_body=raw)
            )

        await store.put(unknown)
        unknown_claim = await store.claim_next(2.0)
        assert unknown_claim is not None and unknown_claim.dispatch_id == unknown.dispatch_id
        async with sessions() as session, session.begin():
            await session.execute(
                update(RecoverableLlmDispatchRow)
                .where(RecoverableLlmDispatchRow.dispatch_id == unknown.dispatch_id)
                .values(upstream_deadline_at=text("CURRENT_TIMESTAMP - INTERVAL '1 second'"))
            )
        assert await restarted.recover_acknowledgement_unknown() == 1
        failed = await restarted.get(unknown.dispatch_id)
        assert failed is not None
        assert failed.state == "FAILED"
        assert failed.generation_count == 1
        assert failed.failure_code == "UPSTREAM_ACKNOWLEDGEMENT_UNKNOWN"
        assert failed.failure_retryable is False
        assert await restarted.claim_next(2.0) is None

        # PostgreSQL, not the host clock, owns ordering.  The table also
        # refuses time inversion even if an application bug attempts it.
        async with sessions() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        update(RecoverableLlmDispatchRow)
                        .where(RecoverableLlmDispatchRow.dispatch_id == first.dispatch_id)
                        .values(updated_at=text("created_at - INTERVAL '1 second'"))
                    )

        async with sessions() as session, session.begin():
            await session.execute(
                update(RecoverableLlmDispatchRow)
                .where(RecoverableLlmDispatchRow.dispatch_id == first.dispatch_id)
                .values(expires_at=text("terminal_at"))
            )
        with pytest.raises(RelayDispatchExpired):
            await restarted.get(first.dispatch_id)
        assert await restarted.scrub_expired() == 1
        async with sessions() as session:
            row = await session.scalar(
                select(RecoverableLlmDispatchRow).where(
                    RecoverableLlmDispatchRow.dispatch_id == first.dispatch_id
                )
            )
            assert row is not None
            assert row.state == "EXPIRED"
            assert row.generation_count == 1
            assert row.request_body is None
            assert row.response_body is None
        with pytest.raises(RelayDispatchExpired):
            await restarted.get(first.dispatch_id)
        with pytest.raises(RelayDispatchExpired):
            await restarted.put(first)
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(RecoverableLlmDispatchRow))
        await sessions.kw["bind"].dispose()


def _request(dispatch_id: str, *, content: str = "postgres relay") -> RelayPutRequest:
    completion: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    value: dict[str, object] = {
        "schema_version": "1.0.0",
        "dispatch_id": dispatch_id,
        "request_sha256": "1" * 64,
        "context_sha256": "2" * 64,
        "completion_sha256": canonical_json_sha256(
            {
                "schema_version": "1.0.0",
                "provider": PROVIDER,
                "model": MODEL,
                "completion": completion,
            }
        ),
        "provider": PROVIDER,
        "model": MODEL,
        "completion": completion,
    }
    body = canonical_response_bytes(value)
    return parse_put_request(
        dispatch_id,
        body,
        provider=PROVIDER,
        model=MODEL,
        maximum_bytes=8192,
    )


def _dispatch_id() -> str:
    return "llmdsp_" + (uuid.uuid4().hex + uuid.uuid4().hex)[:40]


def _database_url() -> str:
    value = os.getenv("WALNUT_TEST_DATABASE_URL")
    if value is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required private relay PostgreSQL coverage"
        )
    return value
