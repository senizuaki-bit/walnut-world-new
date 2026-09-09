"""Dingdang reuses committed teaching facts, without another model call."""

from datetime import UTC, datetime
from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from yaya_agent_build.pipeline import BuildDiagnostic

from tests.integration import test_terminal_read_closure as builds
from tests.integration.test_hint_vertical import _settings
from tests.integration.test_int2_patch_vertical import (
    _materialize_failure_chain,
    _patch_projection_inputs,
)
from walnut_backend.adapters.postgres.models import ProductWorkspaceRow, SkillBuildProvenanceRow
from walnut_backend.adapters.postgres.product_workspaces import refresh_workspace_in_session
from walnut_backend.api.app import create_app


def test_voice_compiler_details_and_marker_without_any_run(tmp_path, monkeypatch):
    database_url = builds._database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        reader = client.app.state.dingdang_voice_context
        observations = []
        original = builds.BuildWorkflowHandler._finish_rejected

        async def observe(self, authority, claim, result):
            async with self._sessions() as db, db.begin():
                session_id = await db.scalar(
                    select(SkillBuildProvenanceRow.session_id).where(
                        SkillBuildProvenanceRow.build_id == authority.build_id,
                    )
                )
                # The build fixture rewrites the initial World directly. Match
                # that setup in its workspace before exercising voice reads.
                updated_at = await db.scalar(
                    select(ProductWorkspaceRow.updated_at).where(
                        ProductWorkspaceRow.session_id == session_id,
                    )
                )
                await refresh_workspace_in_session(
                    db,
                    tenant_id=claim.tenant_id,
                    actor_id=authority.context.actor.actor_id,
                    session_id=session_id,
                    updated_at=max(updated_at, datetime.now(UTC)),
                )
            before = await reader.latest_run_revision(session_id, authority.context)
            await original(self, authority, claim, result)
            after = await reader.latest_run_revision(session_id, authority.context)
            observations.append((before, after, session_id, authority.context))

        monkeypatch.setattr(builds.BuildWorkflowHandler, "_finish_rejected", observe)
        monkeypatch.setattr(
            builds,
            "BuildDiagnostic",
            lambda *_: BuildDiagnostic("COMPILE_ERROR", "main.cpp:8: missing semicolon"),
        )
        terminal = builds._execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=False,
        )
        builds._portal_call(client, _patch_projection_inputs, terminal)
        before, after, session_id, context = observations[0]
        text = builds._portal_call(
            client,
            reader.load,
            session_id,
            context,
            {"code": "x" * 4000, "observation": "y" * 2000},
        )
        assert before[0] is after[0] is None
        assert before != after
        assert "RECTIFICATION" in text and "提示等级=1" in text and "失败次数=1" in text
        assert "missing semicolon" in text
        assert len(text) <= 4000


def test_voice_uses_four_failure_count_already_derived_by_text_workflow(tmp_path, monkeypatch):
    database_url = builds._database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        terminal = builds._execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = builds._activate_and_read_skill(client, terminal)
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        reader = client.app.state.dingdang_voice_context
        query_count = 0

        def count(*_):
            nonlocal query_count
            query_count += 1

        event.listen(Engine, "before_cursor_execute", count)
        started = perf_counter()
        try:
            text = builds._portal_call(client, reader.load, chain.session_id, operation, {})
        finally:
            event.remove(Engine, "before_cursor_execute", count)
        print(f"Dingdang teaching context: {query_count} SQL, {perf_counter() - started:.3f}s")
        assert "失败次数=4" in text and "提示等级=3" in text and "RECTIFICATION" in text
        assert len(text) <= 4000
