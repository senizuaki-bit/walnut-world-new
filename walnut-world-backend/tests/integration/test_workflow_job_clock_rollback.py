"""Workflow timestamps stay causal when PostgreSQL's wall clock moves backwards."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from walnut_backend.adapters.postgres import workflow_jobs as workflow_jobs_module
from walnut_backend.adapters.postgres.models import (
    CommandRow,
    JobStepReceiptRow,
    WorkflowJobRow,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.workflow_jobs import PostgresWorkflowJobStore


def test_workflow_job_timestamps_hold_one_causal_floor_across_clock_rollbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("WALNUT_TEST_DATABASE_URL is required")
    asyncio.run(_exercise_clock_rollbacks(database_url, monkeypatch))


async def _exercise_clock_rollbacks(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = create_session_factory(database_url)
    jobs = PostgresWorkflowJobStore(sessions)
    suffix = uuid.uuid4().hex
    tenant_id = f"tenant_workflow_clock_{suffix}"
    command_id = f"cmd_workflow_clock_{suffix}"
    command_time = datetime(2035, 1, 2, 3, 4, 5, tzinfo=UTC)
    database_times = iter(
        (
            command_time - timedelta(seconds=60),  # enqueue after Command acceptance
            command_time + timedelta(seconds=1),  # first claim
            command_time - timedelta(seconds=10),  # start rollback
            command_time - timedelta(seconds=20),  # first receipt rollback
            command_time - timedelta(seconds=25),  # exact receipt replay rollback
            command_time - timedelta(seconds=30),  # second receipt rollback
            command_time - timedelta(seconds=40),  # retry rollback
            command_time + timedelta(seconds=8),  # retry becomes due
            command_time + timedelta(seconds=7),  # recovered start rollback
            command_time + timedelta(seconds=6),  # terminal rollback
        )
    )

    async def regressing_database_now(_session: AsyncSession) -> datetime:
        return next(database_times)

    monkeypatch.setattr(
        workflow_jobs_module,
        "_database_now",
        regressing_database_now,
    )

    async with sessions() as session, session.begin():
        session.add(
            CommandRow(
                command_id=command_id,
                tenant_id=tenant_id,
                actor_id=f"actor_workflow_clock_{suffix}",
                command_type="TEST_WORKFLOW_CLOCK",
                status="ACCEPTED",
                revision=0,
                terminal=False,
                accepted_at=command_time,
                updated_at=command_time,
                record_json={},
            )
        )
        await session.flush()
        enqueued = await jobs.enqueue_in_session(
            session,
            tenant_id=tenant_id,
            command_id=command_id,
            operation="TEST_WORKFLOW_CLOCK",
            subject_type="CLOCK_FIXTURE",
            subject_id=f"subject_workflow_clock_{suffix}",
            request_sha256="a" * 64,
            job={"fixture": "database-clock-rollback"},
        )
        assert enqueued.created_at == command_time
        assert enqueued.updated_at == command_time
        assert enqueued.next_attempt_at == command_time

    first_claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id="workflow-clock-worker-1",
        lease_seconds=60,
        operation="TEST_WORKFLOW_CLOCK",
    )
    assert first_claim is not None
    first_claim_time = command_time + timedelta(seconds=1)
    assert first_claim.lease_expires_at == first_claim_time + timedelta(seconds=60)

    async with sessions() as session, session.begin():
        running = await jobs.start_step_in_session(
            session,
            first_claim,
            phase="CLOCK_STEP",
            lease_seconds=30,
        )
        assert running.lease_expires_at == first_claim.lease_expires_at

    async with sessions() as session, session.begin():
        first_receipt = await jobs.record_step_in_session(
            session,
            running,
            step_name="CLOCK_RECEIPT_ONE",
            input_sha256="b" * 64,
            output={"result": "stable"},
        )
    assert first_receipt.created is True
    assert first_receipt.completed_at == first_claim_time

    async with sessions() as session, session.begin():
        replayed_receipt = await jobs.record_step_in_session(
            session,
            running,
            step_name="CLOCK_RECEIPT_ONE",
            input_sha256="b" * 64,
            output={"result": "stable"},
        )
    assert replayed_receipt.created is False
    assert replayed_receipt.receipt_id == first_receipt.receipt_id
    assert replayed_receipt.completed_at == first_receipt.completed_at
    assert replayed_receipt.fencing_token == first_receipt.fencing_token

    async with sessions() as session, session.begin():
        second_receipt = await jobs.record_step_in_session(
            session,
            running,
            step_name="CLOCK_RECEIPT_TWO",
            input_sha256="c" * 64,
            output={"result": "also-stable"},
        )
    assert second_receipt.completed_at == first_claim_time

    async with sessions() as session, session.begin():
        await jobs.retry_in_session(
            session,
            running,
            delay_seconds=7,
            phase="CLOCK_RETRY",
            error={"code": "CLOCK_ROLLBACK"},
        )
        retry_row = await _workflow_row(session, tenant_id, command_id)
        assert retry_row.updated_at == first_claim_time
        assert retry_row.next_attempt_at == first_claim_time + timedelta(seconds=7)

    second_claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id="workflow-clock-worker-2",
        lease_seconds=20,
        operation="TEST_WORKFLOW_CLOCK",
    )
    assert second_claim is not None
    second_claim_time = command_time + timedelta(seconds=8)
    assert second_claim.fencing_token == first_claim.fencing_token + 1
    assert second_claim.lease_expires_at == second_claim_time + timedelta(seconds=20)

    async with sessions() as session, session.begin():
        recovered = await jobs.start_step_in_session(
            session,
            second_claim,
            phase="CLOCK_RECOVERED",
            lease_seconds=10,
        )
        assert recovered.lease_expires_at == second_claim.lease_expires_at

    async with sessions() as session, session.begin():
        await jobs.finish_in_session(
            session,
            recovered,
            status="SUCCEEDED",
        )
        terminal = await _workflow_row(session, tenant_id, command_id)
        receipt_times = list(
            await session.scalars(
                select(JobStepReceiptRow.completed_at).where(
                    JobStepReceiptRow.tenant_id == tenant_id,
                    JobStepReceiptRow.job_id == terminal.job_id,
                )
            )
        )
        assert terminal.status == "SUCCEEDED"
        assert terminal.created_at == command_time
        assert terminal.updated_at == second_claim_time
        assert terminal.updated_at >= terminal.created_at
        assert all(completed_at <= terminal.updated_at for completed_at in receipt_times)
        assert terminal.lease_owner is None
        assert terminal.lease_expires_at is None
        assert terminal.next_attempt_at is None

    with pytest.raises(StopIteration):
        next(database_times)
    engine = sessions.kw["bind"]
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()


async def _workflow_row(
    session: AsyncSession,
    tenant_id: str,
    command_id: str,
) -> WorkflowJobRow:
    row = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == tenant_id,
            WorkflowJobRow.command_id == command_id,
        )
    )
    assert row is not None
    return row
