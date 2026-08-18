"""PostgreSQL authority for immutable recoverable relay dispatch resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from walnut_backend.adapters.postgres.models import RecoverableLlmDispatchRow

from .protocol import (
    RelayDispatchConflict,
    RelayDispatchExpired,
    RelayPutRequest,
    RelayResource,
    parse_put_request,
)
from .upstream import ProviderHttpResponse


@dataclass(frozen=True, slots=True)
class StorePutResult:
    resource: RelayResource
    created: bool


class RelayGenerationLimitExceeded(RuntimeError):
    """The explicit global Provider generation budget is exhausted."""


class RelayStore(Protocol):
    async def put(
        self,
        request: RelayPutRequest,
        *,
        max_total_generations: int | None = None,
    ) -> StorePutResult: ...

    async def get(self, dispatch_id: str) -> RelayResource | None: ...

    async def claim_next(
        self,
        upstream_deadline_seconds: float,
        *,
        max_total_generations: int | None = None,
    ) -> RelayResource | None: ...

    async def complete_response(
        self,
        claim: RelayResource,
        response: ProviderHttpResponse,
    ) -> RelayResource: ...

    async def complete_failure(
        self,
        claim: RelayResource,
        *,
        code: str,
        retryable: bool,
    ) -> RelayResource: ...

    async def recover_acknowledgement_unknown(self) -> int: ...

    async def scrub_expired(self) -> int: ...

    async def statistics(self) -> dict[str, object]: ...


class PostgresRelayStore:
    """Linearizable row-locked relay store in the sole backend migration chain."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        result_retention_seconds: int,
    ) -> None:
        if not 1 <= result_retention_seconds <= 315_360_000:
            raise ValueError("result_retention_seconds is outside protocol bounds")
        self._sessions = sessions
        self._retention = timedelta(seconds=result_retention_seconds)

    async def put(
        self,
        request: RelayPutRequest,
        *,
        max_total_generations: int | None = None,
    ) -> StorePutResult:
        _validate_generation_limit(max_total_generations)
        async with self._sessions() as session, session.begin():
            if max_total_generations is not None:
                await _lock_generation_budget(session)
            now = await _database_now(session)
            statement = (
                postgres_insert(RecoverableLlmDispatchRow)
                .values(
                    dispatch_id=request.dispatch_id,
                    request_sha256=request.request_sha256,
                    context_sha256=request.context_sha256,
                    completion_sha256=request.completion_sha256,
                    provider=request.provider,
                    model=request.model,
                    request_body_sha256=request.body_sha256,
                    request_body=request.body,
                    state="PENDING",
                    generation_count=0,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["dispatch_id"])
                .returning(RecoverableLlmDispatchRow.dispatch_id)
            )
            inserted = (await session.scalar(statement)) is not None
            if inserted and max_total_generations is not None:
                reservations = await session.scalar(
                    select(func.count()).select_from(RecoverableLlmDispatchRow)
                )
                if int(reservations or 0) > max_total_generations:
                    # Raising rolls back the new row. Existing dispatches still
                    # replay normally at the limit, but no thirteenth unique
                    # request can become eligible for an upstream generation.
                    raise RelayGenerationLimitExceeded(
                        "global upstream generation limit exhausted before Provider POST"
                    )
            row = await _locked_row(session, request.dispatch_id)
            if row is None:
                raise RuntimeError("relay dispatch disappeared after atomic insert")
            _require_same_request(row, request)
            if row.state == "EXPIRED" or _terminal_expired(row, now):
                raise RelayDispatchExpired("terminal dispatch result has expired")
            return StorePutResult(_resource(row), inserted)

    async def get(self, dispatch_id: str) -> RelayResource | None:
        async with self._sessions() as session, session.begin():
            row = await _locked_row(session, dispatch_id)
            if row is None:
                return None
            now = await _database_now(session)
            if row.state == "EXPIRED" or _terminal_expired(row, now):
                raise RelayDispatchExpired("terminal dispatch result has expired")
            return _resource(row)

    async def claim_next(
        self,
        upstream_deadline_seconds: float,
        *,
        max_total_generations: int | None = None,
    ) -> RelayResource | None:
        if not 0 < upstream_deadline_seconds <= 600:
            raise ValueError("upstream_deadline_seconds is outside safe bounds")
        _validate_generation_limit(max_total_generations)
        async with self._sessions() as session, session.begin():
            if max_total_generations is not None:
                await _lock_generation_budget(session)
            now = await _database_now(session)
            row = await session.scalar(
                select(RecoverableLlmDispatchRow)
                .where(
                    RecoverableLlmDispatchRow.state == "PENDING",
                    RecoverableLlmDispatchRow.generation_count == 0,
                )
                .order_by(
                    RecoverableLlmDispatchRow.created_at,
                    RecoverableLlmDispatchRow.dispatch_id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            if max_total_generations is not None:
                total_generations = await session.scalar(
                    select(
                        func.coalesce(
                            func.sum(RecoverableLlmDispatchRow.generation_count),
                            0,
                        )
                    )
                )
                if int(total_generations or 0) >= max_total_generations:
                    raise RelayGenerationLimitExceeded(
                        "global upstream generation limit exhausted before Provider POST"
                    )
            # Commit the at-most-once generation fence before any network I/O.
            row.generation_count = 1
            row.dispatch_started_at = now
            row.upstream_deadline_at = now + timedelta(seconds=upstream_deadline_seconds)
            row.updated_at = now
            await session.flush()
            return _resource(row)

    async def complete_response(
        self,
        claim: RelayResource,
        response: ProviderHttpResponse,
    ) -> RelayResource:
        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            row = await _locked_row(session, claim.dispatch_id)
            row = _require_claim(row, claim)
            if row.state != "PENDING":
                return _resource(row)
            row.state = "SUCCEEDED"
            row.response_http_status = response.status
            row.response_content_type = response.content_type
            row.response_body = response.body
            row.response_body_sha256 = response.body_sha256
            row.terminal_at = now
            row.expires_at = now + self._retention
            row.updated_at = now
            await session.flush()
            return _resource(row)

    async def complete_failure(
        self,
        claim: RelayResource,
        *,
        code: str,
        retryable: bool,
    ) -> RelayResource:
        if (
            not 1 <= len(code) <= 96
            or not code.replace("_", "").isalnum()
            or code != code.upper()
        ):
            raise ValueError("relay failure code is invalid")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be boolean")
        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            row = await _locked_row(session, claim.dispatch_id)
            row = _require_claim(row, claim)
            if row.state != "PENDING":
                return _resource(row)
            _fail_locked(row, code, retryable, now, self._retention)
            await session.flush()
            return _resource(row)

    async def recover_acknowledgement_unknown(self) -> int:
        """Fail overdue in-flight calls without ever issuing another upstream POST."""

        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            rows = list(
                await session.scalars(
                    select(RecoverableLlmDispatchRow)
                    .where(
                        RecoverableLlmDispatchRow.state == "PENDING",
                        RecoverableLlmDispatchRow.generation_count == 1,
                        RecoverableLlmDispatchRow.upstream_deadline_at <= now,
                    )
                    .order_by(RecoverableLlmDispatchRow.upstream_deadline_at)
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            )
            for row in rows:
                _fail_locked(
                    row,
                    "UPSTREAM_ACKNOWLEDGEMENT_UNKNOWN",
                    False,
                    now,
                    self._retention,
                )
            await session.flush()
            return len(rows)

    async def scrub_expired(self) -> int:
        """Scrub retained bytes while preserving permanent dispatch tombstones."""

        async with self._sessions() as session, session.begin():
            now = await _database_now(session)
            rows = list(
                await session.scalars(
                    select(RecoverableLlmDispatchRow)
                    .where(
                        RecoverableLlmDispatchRow.state.in_(("SUCCEEDED", "FAILED")),
                        RecoverableLlmDispatchRow.expires_at <= now,
                    )
                    .order_by(RecoverableLlmDispatchRow.expires_at)
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            )
            for row in rows:
                _expire_locked(row, now)
            await session.flush()
            return len(rows)

    async def statistics(self) -> dict[str, object]:
        """Return identity-only proof with no credentials, prompts, or responses."""

        async with self._sessions() as session:
            rows = list(
                await session.scalars(
                    select(RecoverableLlmDispatchRow).order_by(
                        RecoverableLlmDispatchRow.created_at,
                        RecoverableLlmDispatchRow.dispatch_id,
                    )
                )
            )
        states: dict[str, int] = {}
        dispatches: list[dict[str, object]] = []
        for row in rows:
            resource = _resource(row)
            _validate_resource_integrity(resource)
            states[row.state] = states.get(row.state, 0) + 1
            dispatches.append(
                {
                    "dispatch_id": row.dispatch_id,
                    "request_sha256": row.request_sha256,
                    "context_sha256": row.context_sha256,
                    "completion_sha256": row.completion_sha256,
                    "provider": row.provider,
                    "model": row.model,
                    "state": row.state,
                    "generation_count": row.generation_count,
                    "response_body_sha256": row.response_body_sha256,
                }
            )
        return {
            "schema_version": "1.0.0",
            "protocol": "YAYA_RECOVERABLE_LLM_V1",
            "unique_dispatches": len(rows),
            "total_generations": sum(row.generation_count for row in rows),
            "max_generation_count": max((row.generation_count for row in rows), default=0),
            "states": states,
            "dispatches": dispatches,
        }


async def _locked_row(
    session: AsyncSession,
    dispatch_id: str,
) -> RecoverableLlmDispatchRow | None:
    return await session.scalar(
        select(RecoverableLlmDispatchRow)
        .where(RecoverableLlmDispatchRow.dispatch_id == dispatch_id)
        .with_for_update()
    )


def _require_same_request(row: RecoverableLlmDispatchRow, request: RelayPutRequest) -> None:
    if row.request_body is not None and _sha256(bytes(row.request_body)) != row.request_body_sha256:
        raise RelayDispatchConflict("persisted relay request bytes are corrupt")
    same = (
        row.dispatch_id == request.dispatch_id
        and row.request_sha256 == request.request_sha256
        and row.context_sha256 == request.context_sha256
        and row.completion_sha256 == request.completion_sha256
        and row.provider == request.provider
        and row.model == request.model
        and row.request_body_sha256 == request.body_sha256
        and (row.request_body is None or bytes(row.request_body) == request.body)
    )
    if not same:
        raise RelayDispatchConflict("dispatch identity conflicts with immutable request bytes")


def _require_claim(
    row: RecoverableLlmDispatchRow | None,
    claim: RelayResource,
) -> RecoverableLlmDispatchRow:
    if row is None:
        raise RuntimeError("claimed relay dispatch disappeared")
    if (
        row.generation_count != 1
        or claim.generation_count != 1
        or row.dispatch_id != claim.dispatch_id
        or row.request_body_sha256 != claim.request_body_sha256
        or row.completion_sha256 != claim.completion_sha256
        or row.dispatch_started_at != claim.dispatch_started_at
    ):
        raise RelayDispatchConflict("relay completion does not match its generation fence")
    return row


def _fail_locked(
    row: RecoverableLlmDispatchRow,
    code: str,
    retryable: bool,
    now: datetime,
    retention: timedelta,
) -> None:
    row.state = "FAILED"
    row.failure_code = code
    row.failure_retryable = retryable
    row.terminal_at = now
    row.expires_at = now + retention
    row.updated_at = now


def _expire_locked(row: RecoverableLlmDispatchRow, now: datetime) -> None:
    if row.state not in {"SUCCEEDED", "FAILED"} or row.expires_at is None:
        return
    if row.expires_at > now:
        return
    row.state = "EXPIRED"
    row.request_body = None
    row.response_http_status = None
    row.response_content_type = None
    row.response_body_sha256 = None
    row.response_body = None
    row.failure_code = None
    row.failure_retryable = None
    row.updated_at = now


def _resource(row: RecoverableLlmDispatchRow) -> RelayResource:
    return RelayResource(
        dispatch_id=row.dispatch_id,
        request_sha256=row.request_sha256,
        context_sha256=row.context_sha256,
        completion_sha256=row.completion_sha256,
        provider=row.provider,
        model=row.model,
        request_body_sha256=row.request_body_sha256,
        request_body=bytes(row.request_body) if row.request_body is not None else None,
        state=row.state,  # type: ignore[arg-type]
        generation_count=row.generation_count,
        dispatch_started_at=row.dispatch_started_at,
        upstream_deadline_at=row.upstream_deadline_at,
        response_http_status=row.response_http_status,
        response_content_type=row.response_content_type,
        response_body_sha256=row.response_body_sha256,
        response_body=bytes(row.response_body) if row.response_body is not None else None,
        failure_code=row.failure_code,
        failure_retryable=row.failure_retryable,
        terminal_at=row.terminal_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_resource_integrity(resource: RelayResource) -> None:
    if resource.request_body is not None:
        if _sha256(resource.request_body) != resource.request_body_sha256:
            raise RelayDispatchConflict("persisted relay request bytes are corrupt")
        value = parse_put_request(
            resource.dispatch_id,
            resource.request_body,
            provider=resource.provider,
            model=resource.model,
            maximum_bytes=max(1, len(resource.request_body)),
        )
        _require_same_resource_request(resource, value)
    elif resource.state != "EXPIRED":
        raise RelayDispatchConflict("non-expired relay request bytes are absent")
    if resource.state == "PENDING":
        if resource.generation_count not in {0, 1} or resource.terminal_at is not None:
            raise RelayDispatchConflict("pending relay state is corrupt")
    elif resource.state == "SUCCEEDED":
        if (
            resource.generation_count != 1
            or resource.response_http_status is None
            or resource.response_content_type is None
            or resource.response_body is None
            or resource.response_body_sha256 is None
            or _sha256(resource.response_body) != resource.response_body_sha256
            or resource.terminal_at is None
            or resource.expires_at is None
        ):
            raise RelayDispatchConflict("successful relay state is corrupt")
    elif resource.state == "FAILED":
        if (
            resource.generation_count != 1
            or resource.failure_code is None
            or resource.failure_retryable is None
            or resource.terminal_at is None
            or resource.expires_at is None
        ):
            raise RelayDispatchConflict("failed relay state is corrupt")


def _require_same_resource_request(
    resource: RelayResource,
    request: RelayPutRequest,
) -> None:
    if (
        resource.dispatch_id != request.dispatch_id
        or resource.request_sha256 != request.request_sha256
        or resource.context_sha256 != request.context_sha256
        or resource.completion_sha256 != request.completion_sha256
        or resource.provider != request.provider
        or resource.model != request.model
        or resource.request_body_sha256 != request.body_sha256
    ):
        raise RelayDispatchConflict("persisted relay request authority is corrupt")


def _terminal_expired(row: RecoverableLlmDispatchRow, now: datetime) -> bool:
    return row.state in {"SUCCEEDED", "FAILED"} and row.expires_at is not None and row.expires_at <= now


async def _database_now(session: AsyncSession) -> datetime:
    # CURRENT_TIMESTAMP is fixed at transaction start.  Under READ COMMITTED a
    # later statement may observe a dispatch created after that timestamp (for
    # example after waiting for the capped-generation advisory lock), which
    # would make claim.updated_at precede row.created_at.  Use PostgreSQL's
    # wall clock at this statement so durable timestamp ordering stays valid.
    value = await session.scalar(text("SELECT clock_timestamp()"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("PostgreSQL returned an invalid clock_timestamp()")
    return value


async def _lock_generation_budget(session: AsyncSession) -> None:
    # One stable application-specific bigint serializes capped reservations
    # and claims across relay processes without changing the uncapped default.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": 0x57414C4E55544C4C},
    )


def _validate_generation_limit(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
        raise ValueError("max_total_generations is outside safe bounds")


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


__all__ = [
    "PostgresRelayStore",
    "RelayGenerationLimitExceeded",
    "RelayStore",
    "StorePutResult",
]
