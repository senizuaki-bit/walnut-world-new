"""Public display reads use committed results, with no history-sized read graph."""

from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import event

from tests.integration import test_hint_vertical as hint
from tests.integration.test_int2_patch_vertical import (
    _materialize_failure_chain,
    _restore_evidence_payload,
    _tamper_evidence_payload,
)
from walnut_backend.api.app import create_app


def test_run_and_evidence_display_reads_are_bounded(tmp_path, monkeypatch):
    database_url = hint._database_url()
    with TestClient(create_app(hint._settings(database_url))) as client:
        terminal = hint._execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        operation = hint._student_operation(client, terminal)
        chain = _materialize_failure_chain(client, terminal, operation, count=1)
        feedback = chain.interactions[0]["feedback"]
        paths = [
            f"/v1/runs/{feedback['run_id']}",
            f"/v1/evidence/{feedback['evidence_refs'][0]['evidence_id']}",
        ]
        costs = []
        for path in paths:
            statements = []

            def count(_connection, _cursor, statement, *_args):
                statements.append(statement)

            engine = terminal.sessions.kw["bind"].sync_engine
            event.listen(engine, "before_cursor_execute", count)
            started = perf_counter()
            try:
                response = client.get(path, headers=terminal.headers)
            finally:
                event.remove(engine, "before_cursor_execute", count)
            assert response.status_code == 200, response.text
            costs.append(len(statements))
            print(f"DISPLAY_READ {path}: {len(statements)} SQL, {perf_counter() - started:.3f}s")
            if "/runs/" in path:
                assert response.json()["agent_feedback"] == feedback
            else:
                assert response.json()["evidence_ref"] == feedback["evidence_refs"][0]
        assert all(cost <= 10 for cost in costs), costs

        # A fast read must still see fresh database changes and retain exact
        # error evidence, rather than returning a cached successful response.
        evidence_id = feedback["evidence_refs"][0]["evidence_id"]
        original = hint._portal_call(client, _tamper_evidence_payload, terminal, evidence_id)
        try:
            for path in paths:
                assert client.get(path, headers=terminal.headers).status_code == 500
        finally:
            hint._portal_call(client, _restore_evidence_payload, terminal, evidence_id, original)
        for path in paths:
            assert client.get(path, headers=terminal.headers).status_code == 200
            denied_headers = {
                **terminal.headers,
                "Authorization": f"Bearer {terminal.tenant_id}:another_student",
            }
            assert client.get(path, headers=denied_headers).status_code == 404
