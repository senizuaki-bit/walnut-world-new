"""Compiler details survive the real Build writer and Agent read path.

Use only an isolated WALNUT_TEST_DATABASE_URL. The compiler is simulated;
the real Build worker, PostgreSQL persistence and Agent read adapter run.
"""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from yaya_agent_build.pipeline import _bounded_diagnostics

from tests.integration import test_terminal_read_closure as vertical


@pytest.fixture(scope="module", params=[True, False], ids=["details", "legacy-codes"])
def rejected_snapshots(tmp_path_factory, request):
    database_url = vertical._database_url()
    settings = replace(
        vertical.Settings.for_test(
            contract_path=vertical.DEFAULT_CONTRACT_PATH,
            contract_release_path=vertical.BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    messages = (
        "main.cpp:8:5: error: expected ';' before 'return'",
        "main.cpp:8:5: error: 'moisture' was not declared in this scope",
    )
    snapshots = []
    with TestClient(vertical.create_app(settings)) as client:
        for message in messages:
            diagnostic = _bounded_diagnostics("COMPILE_ERROR", b"", message.encode())[0]
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(vertical, "BuildDiagnostic", lambda *_args, item=diagnostic: item)
                if not request.param:
                    record = vertical.PostgresWorkflowJobStore.record_step_in_session

                    async def legacy_record(self, *args, **kwargs):
                        if kwargs.get("step_name") == "BUILD_REJECTED":
                            kwargs["output"] = dict(kwargs["output"])
                            kwargs["output"].pop("diagnostic_messages", None)
                        return await record(self, *args, **kwargs)

                    patch.setattr(vertical.PostgresWorkflowJobStore, "record_step_in_session", legacy_record)
                terminal = vertical._execute_build(
                    client,
                    database_url=database_url,
                    tmp_path=tmp_path_factory.mktemp("compile-feedback"),
                    monkeypatch=patch,
                    succeed=False,
                )
                result = vertical._portal_call(client, vertical._read_rejected_build_authority, terminal)
                snapshots.append(result["snapshot"])
                response = client.get(f"/v1/evidence/{terminal.evidence_id}", headers=terminal.headers)
                assert response.status_code == 200, response.text
                assert response.json()["payload"]["diagnostic_codes"] == ["COMPILE_ERROR"]
    return messages, snapshots, request.param


def test_different_compiler_errors_keep_shared_failure_count_bucket(rejected_snapshots):
    _, snapshots, _ = rejected_snapshots
    assert snapshots[0].failure_key == snapshots[1].failure_key, (
        "The demo intentionally counts different compiler mistakes together",
        snapshots[0].failure_key,
        snapshots[1].failure_key,
    )


def test_agent_receives_compiler_message_with_legacy_fallback(rejected_snapshots):
    messages, snapshots, keep_messages = rejected_snapshots
    for message, snapshot in zip(messages, snapshots, strict=True):
        if keep_messages:
            assert message in snapshot.diagnostics, ("Agent lost compiler detail", snapshot.diagnostics)
        else:
            assert snapshot.diagnostics == ("COMPILE_ERROR",)
