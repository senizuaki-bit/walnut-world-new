"""Provider continuation reuses the saved failure count without history replay."""
import asyncio
from unittest.mock import patch

from tests.integration.test_turn_execution_durability import (
    _database_url,
    _dispose,
    _FailedSandbox,
    _invocation,
    _patched_authority_loader,
    _seed_execution,
    _seed_terminal_projection_authority,
)
from walnut_backend.adapters.postgres import run_outcomes


def test_saved_outcome_resumes_without_recounting_history():
    async def exercise():
        fixture = await _seed_execution(_database_url())
        try:
            with _patched_authority_loader(fixture):
                result = await _invocation(fixture, _FailedSandbox()).invoke(fixture.request, fixture.context)
            authority, expected, _ = await _seed_terminal_projection_authority(
                fixture, result, failure_count=1, record_final_authority=False,
            )
            with patch.object(run_outcomes, "exact_failure_suffix_count", side_effect=AssertionError("history recounted")):
                # A fresh handler models a worker restart with no in-memory cache.
                for _ in range(2):
                    outcomes = run_outcomes.PostgresRunOutcomeAuthority(fixture.sessions, fixture.jobs, lease_seconds=60)
                    actual = await outcomes.derive(fixture.claim, root_event=authority.event, context=fixture.context)
                    assert actual == expected
        finally:
            await _dispose(fixture.sessions)

    asyncio.run(exercise())
