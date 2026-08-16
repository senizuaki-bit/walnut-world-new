from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import psycopg
from yaya_agent_backend.database import PostgresDatabase
from yaya_agent_backend.student_skill_chain import (
    StudentSkillChainApplication,
    StudentSkillChainWorker,
)
from yaya_agent_backend.wire import ContractSchemaValidator


class StudentChainWorkerRecoveryTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.worker = StudentSkillChainWorker(
            database=cast(PostgresDatabase, object()),
            application=cast(StudentSkillChainApplication, object()),
            validator=cast(ContractSchemaValidator, object()),
            worker_id="worker_recovery_0001",
            artifact_root=Path(self.temporary.name),
            lease_seconds=2,
            poll_ms=10,
        )

    async def test_build_database_failure_retains_claim_for_takeover(self) -> None:
        claim = SimpleNamespace(operation="CREATE_SKILL_BUILD")
        fail_claim = AsyncMock()

        async def claim_one(_worker: object) -> object:
            return claim

        async def build_skill(_worker: object, _claim: object) -> None:
            raise psycopg.OperationalError("database connection interrupted")

        with (
            patch.object(StudentSkillChainWorker, "_claim_one", claim_one),
            patch.object(StudentSkillChainWorker, "_build_skill", build_skill),
            patch.object(StudentSkillChainWorker, "_fail_claim", fail_claim),
        ):
            self.assertTrue(await self.worker.run_once())

        fail_claim.assert_not_awaited()

    async def test_claim_database_outage_does_not_kill_run_forever(self) -> None:
        stop = asyncio.Event()
        calls = 0

        async def run_once(_worker: object) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise psycopg.OperationalError("database unavailable")
            stop.set()
            return False

        with patch.object(StudentSkillChainWorker, "run_once", run_once):
            await asyncio.wait_for(self.worker.run_forever(stop), timeout=1)

        self.assertEqual(calls, 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
