"""Real PostgreSQL context and public voice route; provider is an offline socket."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from yaya_agent_runtime.adapters.doubao_realtime import DoubaoRealtimeAdapter, DoubaoRealtimeConfig

from tests.integration.test_hint_vertical import _settings
from tests.integration.test_product_workspace_lifecycle import (
    _accept_session,
    _database_url,
    _dispose,
)
from tests.integration.test_terminal_read_closure import _portal_call
from tests.unit.test_dingdang_voice import ProviderSocket
from walnut_backend.adapters.postgres.models import (
    CommandRow,
    EvidenceRow,
    ProductContentUnitRow,
    ProductDraftRow,
    RunRow,
    WorldSnapshotRow,
)
from walnut_backend.api.app import create_app


async def prepare_voice_session(database_url):
    fixture = await _accept_session(database_url, student_is_learner=True)
    await fixture.handler.execute(fixture.claim)
    async with fixture.sessions() as db, db.begin():
        content = await db.scalar(
            select(ProductContentUnitRow).where(
                ProductContentUnitRow.tenant_id == fixture.tenant_id,
                ProductContentUnitRow.content_hash == fixture.context.content_ref.content_hash,
            )
        )
        data = copy.deepcopy(content.content_json)
        data["task"].update(
            name="作物浇水",
            goal="按每块地的目标湿度判断是否浇水。",
            story={"opening": "观察试验田。"},
            knowledge_points=["arrays", "conditionals"],
            hint_policy={"max_level": 3},
        )
        content.content_json = data
    return fixture


async def _game_state(fixture):
    async with fixture.sessions() as db:
        counts = [
            await db.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == fixture.tenant_id,
                )
            )
            for model in (CommandRow, RunRow, EvidenceRow)
        ]
        world = await db.scalar(
            select(WorldSnapshotRow.snapshot_json).where(
                WorldSnapshotRow.tenant_id == fixture.tenant_id,
            )
        )
        draft = await db.scalar(
            select(ProductDraftRow.draft_json).where(
                ProductDraftRow.tenant_id == fixture.tenant_id,
            )
        )
    return counts, world, draft


async def _seed_runs(fixture):
    now = datetime.now(UTC)
    async with fixture.sessions() as db, db.begin():
        for label, actor, timestamp in (
            ("own", fixture.actor_id, now),
            ("foreign", "student_other", now + timedelta(seconds=1)),
        ):
            db.add(
                RunRow(
                    run_id=f"run_voice_{label}_{fixture.session_id}",
                    tenant_id=fixture.tenant_id,
                    actor_id=actor,
                    content_hash=fixture.context.content_ref.content_hash,
                    session_id=fixture.session_id,
                    turn_id="turn_voice_fixture",
                    command_id="cmd_voice_fixture",
                    created_at=timestamp,
                    run_json={
                        "run_id": f"run_voice_{label}",
                        "status": "FAILED",
                        "sandbox": {"status": "FAILED", "failure": {"message": f"{label} failure"}},
                        "world_application": {"status": "NOT_ATTEMPTED", "failure": None},
                        "agent_feedback": {"message": f"{label} feedback"},
                        "updated_at": timestamp.isoformat(),
                    },
                )
            )


def test_voice_reads_current_context_without_game_writes():
    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        fixture = _portal_call(client, prepare_voice_session, database_url)
        provider = ProviderSocket()
        client.app.state.dingdang_voice_factory = lambda: DoubaoRealtimeAdapter(
            DoubaoRealtimeConfig(api_key="offline-key"), provider.connect
        )
        reader = client.app.state.dingdang_voice_context
        try:
            empty = _portal_call(client, reader.load, fixture.session_id, fixture.context, {})
            assert "作物浇水" in empty and "null" in empty
            _portal_call(client, _seed_runs, fixture)
            before = _portal_call(client, _game_state, fixture)
            path = f"/product-experience/v1/sessions/{fixture.session_id}/dingdang-voice"
            with client.websocket_connect(path) as ws:
                ws.send_json(
                    {
                        "type": "start",
                        "token": f"{fixture.tenant_id}:{fixture.actor_id}",
                        "context": {"code": "old code"},
                    }
                )
                assert ws.receive_json()["type"] == "voice.ready"
                ws.send_json({"type": "context", "context": {"code": "new code"}})
                assert ws.receive_json()["type"] == "session.updated"
                assert "new code" in provider.sent[-1]["session"]["instructions"]
                ws.send_bytes(bytes(640))
                ws.receive_json()
                context_reply = ws.receive_json()["text"]
                assert "new code" in context_reply and "old code" not in context_reply
                assert "own failure" in context_reply and "foreign" not in context_reply
                assert len(context_reply) <= 4000
                ws.receive_json()
                ws.send_json({"type": "close"})
            after = _portal_call(client, _game_state, fixture)
            assert after == before
            with client.websocket_connect(path) as ws:
                ws.send_json({"type": "start", "token": f"{fixture.tenant_id}:student_other"})
                assert ws.receive_json() == {
                    "type": "voice.error",
                    "code": "VOICE_SESSION_UNAVAILABLE",
                }
        finally:
            _portal_call(client, _dispose, fixture.sessions)


async def _record_success_without_book(fixture):
    now = datetime.now(UTC) + timedelta(seconds=2)
    run_id = f"run_voice_completed_{fixture.session_id}"
    async with fixture.sessions() as db, db.begin():
        db.add(
            RunRow(
                run_id=run_id,
                tenant_id=fixture.tenant_id,
                actor_id=fixture.actor_id,
                content_hash=fixture.context.content_ref.content_hash,
                session_id=fixture.session_id,
                turn_id="turn_voice_completed",
                command_id="cmd_voice_completed",
                created_at=now,
                run_json={
                    "run_id": run_id,
                    "status": "SUCCEEDED",
                    "sandbox": {"status": "SUCCEEDED", "failure": None},
                    "world_application": {"status": "COMMITTED", "failure": None},
                    "agent_feedback": None,
                    "updated_at": now.isoformat(),
                },
            )
        )
    return run_id


async def _finish_book(fixture, run_id):
    async with fixture.sessions() as db, db.begin():
        row = await db.get(RunRow, run_id)
        value = copy.deepcopy(row.run_json)
        value["agent_feedback"] = {"message": "新的成长总结已完成"}
        # The real completion path may reuse the Run's causal timestamp for
        # feedback. Its payload changes even when updated_at stays identical.
        row.run_json = value


def test_voice_run_marker_is_one_query_and_pushes_success_before_book(monkeypatch):
    from walnut_backend.api.routes import dingdang_voice

    monkeypatch.setattr(dingdang_voice, "CONTEXT_POLL_SECONDS", 0.05)
    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        fixture = _portal_call(client, prepare_voice_session, database_url)
        provider = ProviderSocket()
        client.app.state.dingdang_voice_factory = lambda: DoubaoRealtimeAdapter(
            DoubaoRealtimeConfig(api_key="offline-key"), provider.connect
        )
        reader = client.app.state.dingdang_voice_context
        try:
            _portal_call(client, _seed_runs, fixture)
            query_count = 0

            def count_query(*_args):
                nonlocal query_count
                query_count += 1

            event.listen(Engine, "before_cursor_execute", count_query)
            started = perf_counter()
            try:
                for _ in range(5):
                    revision = _portal_call(
                        client, reader.latest_run_revision, fixture.session_id, fixture.context
                    )
                    assert revision[0] == f"run_voice_own_{fixture.session_id}"
            finally:
                event.remove(Engine, "before_cursor_execute", count_query)
            assert query_count == 5
            print(f"voice marker: {query_count} SQL / 5 reads / {perf_counter() - started:.3f}s")

            async def wait_for_updates(count):
                async with asyncio.timeout(3):
                    while sum(item["type"] == "session.update" for item in provider.sent) < count:
                        await asyncio.sleep(0.01)

            path = f"/product-experience/v1/sessions/{fixture.session_id}/dingdang-voice"
            with client.websocket_connect(path) as ws:
                ws.send_json(
                    {
                        "type": "start",
                        "token": f"{fixture.tenant_id}:{fixture.actor_id}",
                        "context": {"code": "current editor code"},
                    }
                )
                assert ws.receive_json()["type"] == "voice.ready"
                started = perf_counter()
                run_id = _portal_call(client, _record_success_without_book, fixture)
                client.portal.call(wait_for_updates, 1)
                assert ws.receive_json()["type"] == "session.updated"
                instructions = provider.sent[-1]["session"]["instructions"]
                assert "本次任务已完成" in instructions
                assert "尚未生成，不影响已确认的运行结果" in instructions
                assert "IN_PROGRESS" not in instructions
                assert "current editor code" in instructions
                print(f"voice committed success -> session.update: {perf_counter() - started:.3f}s")

                _portal_call(client, _finish_book, fixture, run_id)
                client.portal.call(wait_for_updates, 2)
                assert ws.receive_json()["type"] == "session.updated"
                instructions = provider.sent[-1]["session"]["instructions"]
                assert "新的成长总结已完成" in instructions
                assert not any(item["type"] == "conversation.item.create" for item in provider.sent)
                ws.send_json({"type": "close"})
        finally:
            _portal_call(client, _dispose, fixture.sessions)
