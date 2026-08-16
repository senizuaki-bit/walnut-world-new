"""Background dispatcher for fenced, at-most-once upstream generations."""

from __future__ import annotations

import asyncio

from .protocol import RelayResource
from .store import RelayStore
from .upstream import (
    UpstreamAcknowledgementUnknown,
    UpstreamResponseInvalid,
    UpstreamTransport,
)


class RelayDispatcher:
    def __init__(
        self,
        store: RelayStore,
        upstream: UpstreamTransport,
        *,
        upstream_deadline_seconds: float,
        idle_poll_seconds: float,
        max_total_generations: int | None = None,
    ) -> None:
        if not 0 < upstream_deadline_seconds <= 600:
            raise ValueError("upstream_deadline_seconds is outside safe bounds")
        if not 0.01 <= idle_poll_seconds <= 60:
            raise ValueError("idle_poll_seconds is outside safe bounds")
        if max_total_generations is not None and (
            isinstance(max_total_generations, bool)
            or not isinstance(max_total_generations, int)
            or not 1 <= max_total_generations <= 1_000_000
        ):
            raise ValueError("max_total_generations is outside safe bounds")
        self._store = store
        self._upstream = upstream
        self._deadline = upstream_deadline_seconds
        self._poll = idle_poll_seconds
        self._max_total_generations = max_total_generations

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Drain pending dispatches until the caller requests shutdown."""

        while not stop.is_set():
            progressed = await self.run_once()
            if not progressed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)
                except TimeoutError:
                    pass

    async def run_once(self) -> bool:
        # A previous process may have died after the only POST.  The Provider
        # has no client-identity lookup, so the only correct recovery is loud,
        # terminal uncertainty rather than a second POST.
        recovered = await self._store.recover_acknowledgement_unknown()
        await self._store.scrub_expired()
        claim = await self._store.claim_next(
            self._deadline,
            max_total_generations=self._max_total_generations,
        )
        if claim is None:
            return recovered > 0
        await self._dispatch_claim(claim)
        return True

    async def _dispatch_claim(self, claim: RelayResource) -> None:
        try:
            response = await self._upstream.post_completion(claim.completion)
        except UpstreamAcknowledgementUnknown:
            # Leave the generation fenced PENDING.  The deadline recovery path
            # terminalizes it without issuing another upstream generation.
            return
        except UpstreamResponseInvalid:
            await self._store.complete_failure(
                claim,
                code="UPSTREAM_RESPONSE_INVALID",
                retryable=False,
            )
            return
        except Exception:
            # Unknown transport implementations receive the same conservative
            # treatment: once generation_count reached one, no retry is safe.
            return
        await self._store.complete_response(claim, response)


__all__ = ["RelayDispatcher"]
