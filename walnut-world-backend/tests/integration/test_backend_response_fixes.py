"""Concrete concurrency, history, read reuse and scheduling regressions."""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration import test_hint_vertical as hint
from walnut_backend.adapters.postgres.agent_runtime import PostgresAgentRuntimeReads
from walnut_backend.adapters.postgres.models import CommandRow, ProductDraftRow, ProductWorkspaceRow
from walnut_backend.adapters.postgres.product_drafts import PostgresProductDraftStore
from walnut_backend.adapters.postgres.product_workspaces import PostgresProductWorkspaceStore
from walnut_backend.adapters.postgres.session import create_session_factory, snapshot_read
from walnut_backend.adapters.postgres.workflow_jobs import PostgresWorkflowJobStore
from walnut_backend.api.app import create_app
from walnut_backend.workers.workflow_worker import WorkflowWorker


def test_workspace_save_during_read_is_consistent(tmp_path, monkeypatch):
    database_url = hint._database_url()
    with TestClient(create_app(hint._settings(database_url))) as client:
        terminal = hint._execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        context = hint._student_operation(client, terminal)
        sid, *_ = hint._portal_call(client, hint._prepare_failed_run, terminal)

        async def exercise():
            async def save():
                async with terminal.sessions() as db:
                    draft = await db.scalar(
                        select(ProductDraftRow).where(
                            ProductDraftRow.tenant_id == terminal.tenant_id,
                            ProductDraftRow.session_id == sid,
                        )
                    )
                    body = {
                        key: deepcopy(draft.draft_json[key])
                        for key in (
                            "session_id",
                            "draft_id",
                            "skill_id",
                            "content_ref",
                            "display_name",
                            "source_bundle",
                        )
                    }
                    body.update(base_revision=draft.revision, base_draft_sha256=draft.draft_sha256)
                saved = await PostgresProductDraftStore(terminal.sessions).upsert(
                    sid,
                    body["draft_id"],
                    body,
                    json.dumps(body).encode(),
                    f"idem_save_{uuid4().hex}",
                    context,
                )
                assert saved.ok

            # The build fixture advances durable facts directly. Publish the
            # matching workspace through the normal draft-save path first.
            await save()
            before = await PostgresProductWorkspaceStore(terminal.sessions).get(sid, context)
            assert before.ok

            class Interleaved(AsyncSession):
                saved = False

                async def scalar(self, statement, *args, **kwargs):
                    row = await super().scalar(statement, *args, **kwargs)
                    if isinstance(row, ProductWorkspaceRow) and not self.saved:
                        self.saved = True
                        await save()
                    return row

            readers = async_sessionmaker(
                terminal.sessions.kw["bind"], class_=Interleaved, expire_on_commit=False
            )
            during = await PostgresProductWorkspaceStore(readers).get(sid, context)
            assert during.ok
            after = await PostgresProductWorkspaceStore(terminal.sessions).get(sid, context)
            assert after.ok
            assert after.value["workspace_revision"] > during.value["workspace_revision"]

        hint._portal_call(client, exercise)


def test_history_reaches_next_hint_but_excludes_current_and_other_actors(tmp_path, monkeypatch):
    database_url = hint._database_url()
    with TestClient(create_app(hint._settings(database_url))) as client:
        terminal = hint._execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        origin = hint._student_operation(client, terminal)
        sid, _, revision, sequence, turn_sequence = hint._portal_call(
            client, hint._prepare_failed_run, terminal
        )
        hint._portal_call(client, hint._patch_projection_inputs, terminal)
        reads = PostgresAgentRuntimeReads(terminal.sessions)

        class RecordingProvider(hint._HintReplyProvider):
            async def dispatch(self, identity, request, context):
                self.prompt = repr(request)
                return await super().dispatch(identity, request, context)

        for index, marker in enumerate(("history_marker_alpha", "history_marker_beta")):
            tag = uuid4().hex
            operation = replace(
                origin,
                command_id=f"cmd_{tag}",
                request_id=f"req_{tag}",
                trace_id=f"trace_{tag}",
                correlation_id=f"corr_{tag}",
            )
            payload = hint._hint_payload(
                revision=revision,
                sequence=sequence,
                turn_sequence=turn_sequence + index,
                turn_id=f"turn_{tag}",
                skill_bindings=[],
            )
            payload["input"]["text"] = marker
            accepted = hint._portal_call(
                client,
                client.app.state.agent_turns.accept,
                sid,
                json.dumps(payload).encode(),
                f"idem_{tag}",
                operation,
            )
            assert accepted.ok
            history = hint._portal_call(client, reads.list_recent, sid, 8, operation)
            assert len(history) == index
            assert all(marker not in item.message for item in history)
            other = replace(
                operation, actor=replace(operation.actor, actor_id="student_other_0001")
            )
            assert hint._portal_call(client, reads.list_recent, sid, 8, other) == ()
            assert (
                hint._portal_call(client, reads.list_recent, "session_other_0001", 8, operation)
                == ()
            )
            provider = RecordingProvider()
            hint._run_hint_turn(client, terminal, operation, accepted, provider)
            if index:
                assert "history_marker_alpha" in provider.prompt
                assert "学生：" in history[0].message and "叮当：" in history[0].message


async def _seed_jobs(sessions, jobs, tenant, operations):
    now = datetime.now(UTC)
    ids = []
    async with sessions() as db, db.begin():
        for operation, request in operations:
            cid = f"cmd_{uuid4().hex}"
            db.add(
                CommandRow(
                    command_id=cid,
                    tenant_id=tenant,
                    actor_id="student_test_0001",
                    command_type=operation,
                    status="ACCEPTED",
                    revision=0,
                    terminal=False,
                    accepted_at=now,
                    updated_at=now,
                    record_json={},
                )
            )
            await db.flush()
            job = await jobs.enqueue_in_session(
                db,
                tenant_id=tenant,
                command_id=cid,
                operation=operation,
                subject_type="TEST",
                subject_id=cid,
                request_sha256="a" * 64,
                job={"request": request},
            )
            ids.append(job.job_id)
    return ids


def test_hint_lane_runs_while_background_is_still_busy():
    async def exercise():
        sessions = create_session_factory(os.environ["WALNUT_TEST_DATABASE_URL"])
        jobs = PostgresWorkflowJobStore(sessions)
        tenant = f"tenant_lane_{uuid4().hex}"
        started, release, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        try:
            ids = await _seed_jobs(
                sessions,
                jobs,
                tenant,
                [
                    ("CREATE_SKILL_BUILD", {}),
                    ("EXECUTE_AGENT_TURN", {"input": {"type": "MESSAGE"}, "skill_bindings": []}),
                    ("EXECUTE_AGENT_TURN", {"input": {"type": "MESSAGE"}, "skill_bindings": [{}]}),
                ],
            )

            class Handler:
                operations = frozenset({"CREATE_SKILL_BUILD", "EXECUTE_AGENT_TURN"})

                async def execute(self, claim):
                    if claim.job_id == ids[0]:
                        started.set()
                        await release.wait()
                    else:
                        assert claim.job_id == ids[1]
                        answered.set()

            workers = [
                WorkflowWorker(
                    session_factory=sessions,
                    jobs=jobs,
                    commands=None,
                    handlers=(Handler(),),
                    worker_id=lane,
                    lane=lane,
                )
                for lane in ("background", "interactive")
            ]
            async def run_background():
                # Like the production worker, poll until a newly enqueued job
                # becomes eligible on the database clock. A single empty poll
                # must not leave this test waiting for a handler that never ran.
                while not await workers[0].run_once(tenant):
                    await asyncio.sleep(0.01)

            busy = asyncio.create_task(run_background())
            try:
                await asyncio.wait_for(started.wait(), 2)
                assert await asyncio.wait_for(workers[1].run_once(tenant), 2)
                assert answered.is_set() and not busy.done()
                assert not await workers[1].run_once(tenant)
                remaining = await jobs.claim_next(
                    tenant_id=tenant, worker_id="other", lease_seconds=60, lane="background"
                )
                assert remaining.job_id == ids[2]  # A bound MESSAGE is a Run, not a Hint.
            finally:
                release.set()
                if not started.is_set():
                    busy.cancel()
                    await asyncio.gather(busy, return_exceptions=True)
                else:
                    await busy
        finally:
            await sessions.kw["bind"].dispose()

    asyncio.run(exercise())


def test_snapshot_query_reuse_keeps_parameters_and_request_boundaries():
    async def exercise():
        sessions = create_session_factory(os.environ["WALNUT_TEST_DATABASE_URL"])
        jobs = PostgresWorkflowJobStore(sessions)
        tenant = f"tenant_read_{uuid4().hex}"
        queries = []

        def count(conn, cursor, statement, params, context, many):
            if statement.startswith("SELECT"):
                queries.append(statement)

        try:
            await _seed_jobs(sessions, jobs, tenant, [("FIRST", {}), ("SECOND", {})])
            event.listen(sessions.kw["bind"].sync_engine, "before_cursor_execute", count)
            async with snapshot_read(sessions) as db:
                statement = select(CommandRow).where(CommandRow.tenant_id == tenant)
                first = await db.scalar(statement.where(CommandRow.command_type == "FIRST"))
                again = await db.scalar(statement.where(CommandRow.command_type == "FIRST"))
                second = await db.scalar(statement.where(CommandRow.command_type == "SECOND"))
                assert first.command_id == again.command_id != second.command_id
                assert len(queries) == 2
                async with sessions() as writer, writer.begin():
                    await writer.execute(
                        update(CommandRow)
                        .where(CommandRow.command_id == first.command_id)
                        .values(record_json={"changed": True})
                    )
                assert (
                    await db.scalar(statement.where(CommandRow.command_type == "FIRST"))
                ).record_json == {}
            async with snapshot_read(sessions) as db:
                assert (
                    await db.scalar(statement.where(CommandRow.command_type == "FIRST"))
                ).record_json == {"changed": True}
        finally:
            event.remove(sessions.kw["bind"].sync_engine, "before_cursor_execute", count)
            await sessions.kw["bind"].dispose()

    asyncio.run(exercise())
