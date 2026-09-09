"""Short model waits finish in one call; long waits remain recoverable."""
import asyncio
from dataclasses import replace

import pytest

from tests.integration.test_turn_execution_durability import (
    _database_url,
    _dispose,
    _llm_request,
    _ReplyProvider,
    _seed_execution,
)
from walnut_backend.adapters.postgres.durable_llm import (
    DurableLlmDispatchPending,
    PostgresDurableLlm,
)


class PendingProvider(_ReplyProvider):
    hang = False

    async def dispatch(self, identity, request, context):
        ready = await super().dispatch(identity, request, context)
        return replace(ready, state="PENDING", result=None, raw_response_sha256=None, retry_after_seconds=1)

    async def reconcile(self, identity, request, context):
        if self.hang:
            await asyncio.sleep(60)
        return await super().reconcile(identity, request, context)


@pytest.mark.parametrize("wait_seconds,hang", [(2.0, False), (0.1, False), (1.1, True)])
def test_short_wait_keeps_one_dispatch_and_can_resume(wait_seconds, hang):
    async def exercise():
        fixture = await _seed_execution(_database_url())
        provider = PendingProvider()
        provider.hang = hang
        request = replace(_llm_request(fixture.versions), timeout_ms=5000)

        def adapter(wait):
            return PostgresDurableLlm(
                session_factory=fixture.sessions, jobs=fixture.jobs, claim=fixture.claim,
                provider=provider, provider_name="fake-provider", model_version="fake-model-v1",
                lease_seconds=60, pending_wait_seconds=wait,
            )

        try:
            if wait_seconds < 1 or hang:
                with pytest.raises(DurableLlmDispatchPending):
                    await adapter(wait_seconds).generate(request, fixture.context)
                assert provider.reconciliations == 0
                provider.hang = False
                result = await adapter(0).generate(request, fixture.context)
            else:
                result = await adapter(wait_seconds).generate(request, fixture.context)
            replay = await adapter(0).generate(request, fixture.context)
            assert result == replay
            assert provider.calls == 1
        finally:
            await _dispose(fixture.sessions)

    asyncio.run(exercise())
