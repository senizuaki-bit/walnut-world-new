from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from walnut_backend.llm_relay.app import create_relay_app
from walnut_backend.llm_relay.config import RelaySettings
from walnut_backend.llm_relay.dispatcher import RelayDispatcher
from walnut_backend.llm_relay.protocol import (
    DISPATCH_PATH,
    PROTOCOL,
    RelayDispatchConflict,
    RelayDispatchExpired,
    RelayPutRequest,
    RelayResource,
    canonical_response_bytes,
    parse_put_request,
)
from walnut_backend.llm_relay.store import (
    RelayGenerationLimitExceeded,
    RelayStore,
    StorePutResult,
)
from walnut_backend.llm_relay.upstream import (
    ProviderHttpResponse,
    UpstreamAcknowledgementUnknown,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
RELAY_KEY = "relay-only-test-key"
UPSTREAM_KEY = "upstream-only-test-key"
PROVIDER = "deepseek"
MODEL = "deepseek-v4-flash"
DISPATCH_ID = "llmdsp_" + "a" * 40


def test_settings_keep_relay_and_upstream_credentials_separate_and_secret() -> None:
    settings = _settings()
    rendered = repr(settings)
    assert RELAY_KEY not in rendered
    assert UPSTREAM_KEY not in rendered

    values = _settings_env()
    assert RelaySettings.from_env(values) == settings
    assert settings.max_total_generations is None
    values["WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS"] = "12"
    assert RelaySettings.from_env(values) == replace(settings, max_total_generations=12)
    values["WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS"] = "0"
    with pytest.raises(ValueError, match="max_total_generations"):
        RelaySettings.from_env(values)
    values.pop("WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS")
    values["WALNUT_LLM_UPSTREAM_API_KEY"] = RELAY_KEY
    try:
        RelaySettings.from_env(values)
    except ValueError as error:
        assert "must be different" in str(error)
    else:
        raise AssertionError("same relay and upstream credential was accepted")


def test_settings_accept_only_acl_controlled_bounded_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "provider.key"
    secret_file.write_bytes((UPSTREAM_KEY + "\r\n").encode("utf-8"))
    _harden_test_secret_file(secret_file)
    values = _settings_env()
    values.pop("WALNUT_LLM_UPSTREAM_API_KEY")
    values["WALNUT_LLM_UPSTREAM_API_KEY_FILE"] = str(secret_file)

    assert RelaySettings.from_env(values).upstream_api_key == UPSTREAM_KEY

    values["WALNUT_LLM_UPSTREAM_API_KEY"] = UPSTREAM_KEY
    with pytest.raises(ValueError, match="set exactly one"):
        RelaySettings.from_env(values)
    values.pop("WALNUT_LLM_UPSTREAM_API_KEY")

    _grant_broad_test_read(secret_file)
    with pytest.raises(ValueError, match="deny") as broad_error:
        RelaySettings.from_env(values)
    assert UPSTREAM_KEY not in str(broad_error.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
def test_settings_reject_windows_null_dacl(tmp_path: Path) -> None:
    secret_file = tmp_path / "provider.key"
    secret_file.write_text(UPSTREAM_KEY, encoding="utf-8")
    _set_null_test_dacl(secret_file)
    values = _settings_env()
    values.pop("WALNUT_LLM_UPSTREAM_API_KEY")
    values["WALNUT_LLM_UPSTREAM_API_KEY_FILE"] = str(secret_file)

    try:
        with pytest.raises(ValueError, match="deny") as null_dacl_error:
            RelaySettings.from_env(values)
        assert str(secret_file) not in str(null_dacl_error.value)
    finally:
        _harden_test_secret_file(secret_file)


def test_missing_secret_file_path_is_not_disclosed_in_traceback(tmp_path: Path) -> None:
    secret_file = tmp_path / "private-provider-key-must-not-appear"
    environment = dict(os.environ)
    environment.update(_settings_env())
    environment["PYTHONPATH"] = str(BACKEND_ROOT / "src")
    environment.pop("WALNUT_LLM_UPSTREAM_API_KEY")
    environment["WALNUT_LLM_UPSTREAM_API_KEY_FILE"] = str(secret_file)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from walnut_backend.llm_relay.config import RelaySettings; "
                "RelaySettings.from_env()"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode != 0
    assert "WALNUT_LLM_UPSTREAM_API_KEY_FILE is unavailable" in completed.stderr
    assert str(secret_file) not in completed.stderr
    assert secret_file.name not in completed.stderr


def test_settings_reject_oversized_or_non_secret_file_text(tmp_path: Path) -> None:
    secret_file = tmp_path / "provider.key"
    secret_file.write_bytes(b"x" * 4099)
    _harden_test_secret_file(secret_file)
    values = _settings_env()
    values.pop("WALNUT_LLM_UPSTREAM_API_KEY")
    values["WALNUT_LLM_UPSTREAM_API_KEY_FILE"] = str(secret_file)

    with pytest.raises(ValueError, match="too large"):
        RelaySettings.from_env(values)

    secret_file.write_bytes(b"synthetic key with spaces")
    with pytest.raises(ValueError, match="non-whitespace secret"):
        RelaySettings.from_env(values)


def test_protocol_rejects_noncanonical_conflicts_and_completion_hash_drift() -> None:
    body = _request_body()
    request = parse_put_request(
        DISPATCH_ID,
        body,
        provider=PROVIDER,
        model=MODEL,
        maximum_bytes=8192,
    )
    assert request.body == body
    assert request.body_sha256 == hashlib.sha256(body).hexdigest()

    spaced = json.dumps(json.loads(body), ensure_ascii=False).encode()
    try:
        parse_put_request(
            DISPATCH_ID,
            spaced,
            provider=PROVIDER,
            model=MODEL,
            maximum_bytes=8192,
        )
    except ValueError as error:
        assert "canonical" in str(error)
    else:
        raise AssertionError("noncanonical request bytes were accepted")

    changed = json.loads(body)
    changed["completion_sha256"] = "0" * 64
    try:
        parse_put_request(
            DISPATCH_ID,
            canonical_response_bytes(changed),
            provider=PROVIDER,
            model=MODEL,
            maximum_bytes=8192,
        )
    except ValueError as error:
        assert "completion hash" in str(error)
    else:
        raise AssertionError("completion hash drift was accepted")


def test_protocol_accepts_client_decimal_completion_hash() -> None:
    from yaya_agent_runtime import llm_recovery_sha256

    completion: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "diagnostic"}],
        "temperature": 0.2,
        "max_tokens": 64,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "agent_output",
                "strict": True,
                "schema": {
                    "type": "number",
                    "minimum": -0.3,
                    "maximum": 0.3,
                    "multipleOf": 0.000001,
                },
            },
        },
        "stream": False,
    }
    completion_sha256 = llm_recovery_sha256(
        {
            "schema_version": "1.0.0",
            "provider": PROVIDER,
            "model": MODEL,
            "completion": completion,
        }
    )
    value = {
        "schema_version": "1.0.0",
        "dispatch_id": DISPATCH_ID,
        "request_sha256": "1" * 64,
        "context_sha256": "2" * 64,
        "completion_sha256": completion_sha256,
        "provider": PROVIDER,
        "model": MODEL,
        "completion": completion,
    }

    parsed = parse_put_request(
        DISPATCH_ID,
        canonical_response_bytes(value),
        provider=PROVIDER,
        model=MODEL,
        maximum_bytes=8192,
    )

    assert parsed.completion_sha256 == completion_sha256


def test_protocol_rejects_legacy_undeclared_choice_count_even_with_matching_hash() -> None:
    from yaya_agent_runtime import llm_recovery_sha256

    value = json.loads(_request_body())
    completion = value["completion"]
    assert isinstance(completion, dict)
    completion["n"] = 1
    value["completion_sha256"] = llm_recovery_sha256(
        {
            "schema_version": "1.0.0",
            "provider": PROVIDER,
            "model": MODEL,
            "completion": completion,
        }
    )

    with pytest.raises(ValueError, match="completion fields are not closed"):
        parse_put_request(
            DISPATCH_ID,
            canonical_response_bytes(value),
            provider=PROVIDER,
            model=MODEL,
            maximum_bytes=8192,
        )


def test_dispatcher_fences_before_post_and_never_reposts_acknowledgement_unknown() -> None:
    request = _put_request()
    store = MemoryRelayStore()
    asyncio.run(store.put(request))
    upstream = UnknownAckUpstream()
    first = RelayDispatcher(store, upstream, upstream_deadline_seconds=0.02, idle_poll_seconds=0.01)

    assert asyncio.run(first.run_once()) is True
    assert upstream.posts == 1
    assert store.resource.generation_count == 1
    assert store.resource.state == "PENDING"

    # A fresh process uses the durable generation fence.  It never calls the
    # upstream transport again, even after the uncertainty deadline expires.
    store.resource = replace(
        store.resource,
        upstream_deadline_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    restarted = RelayDispatcher(store, upstream, upstream_deadline_seconds=0.02, idle_poll_seconds=0.01)
    assert asyncio.run(restarted.run_once()) is True
    assert upstream.posts == 1
    assert store.resource.state == "FAILED"
    assert store.resource.failure_code == "UPSTREAM_ACKNOWLEDGEMENT_UNKNOWN"
    assert store.resource.failure_retryable is False


def test_dispatcher_loop_survives_one_store_failure_before_claim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        store = OneShotMaintenanceFailureStore()
        await store.put(_put_request(), max_total_generations=24)
        upstream = CountingSuccessUpstream()
        dispatcher = RelayDispatcher(
            store,
            upstream,
            upstream_deadline_seconds=0.1,
            idle_poll_seconds=0.01,
            max_total_generations=24,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(dispatcher.run_forever(stop))
        try:
            for _ in range(200):
                resource = store.resources[DISPATCH_ID]
                if resource.state == "SUCCEEDED":
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError("dispatcher did not recover from the transient store failure")
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1)

        resource = store.resources[DISPATCH_ID]
        assert store.maintenance_calls >= 2
        assert store.observed_limits == {24}
        assert resource.state == "SUCCEEDED"
        assert resource.generation_count == 1
        assert upstream.posts == 1

    asyncio.run(exercise())
    assert "exception_type=ConnectionError" in caplog.text
    assert "synthetic transient store failure" not in caplog.text


def test_dispatcher_loop_never_reposts_after_completion_store_failure() -> None:
    async def exercise() -> None:
        store = OneShotCompletionFailureStore()
        await store.put(_put_request())
        upstream = CountingSuccessUpstream()
        dispatcher = RelayDispatcher(
            store,
            upstream,
            upstream_deadline_seconds=0.02,
            idle_poll_seconds=0.01,
            max_total_generations=24,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(dispatcher.run_forever(stop))
        try:
            for _ in range(200):
                resource = store.resource
                if (
                    resource is not None
                    and store.completion_calls == 1
                    and resource.state == "PENDING"
                    and resource.generation_count == 1
                ):
                    # Make the acknowledgement-unknown recovery boundary
                    # deterministic without depending on host clock granularity.
                    store.resource = replace(
                        resource,
                        upstream_deadline_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                if resource is not None and resource.state == "FAILED":
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError("generation-fenced completion did not terminalize")
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1)

        assert store.resource is not None
        assert store.completion_calls == 1
        assert store.resource.state == "FAILED"
        assert store.resource.generation_count == 1
        assert store.resource.failure_code == "UPSTREAM_ACKNOWLEDGEMENT_UNKNOWN"
        assert upstream.posts == 1

    asyncio.run(exercise())


def test_dispatcher_loop_does_not_swallow_task_cancellation() -> None:
    async def exercise() -> None:
        store = GenerationBudgetMemoryStore()
        upstream = CountingSuccessUpstream()
        dispatcher = RelayDispatcher(
            store,
            upstream,
            upstream_deadline_seconds=0.1,
            idle_poll_seconds=60,
            max_total_generations=24,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(dispatcher.run_forever(stop))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upstream.posts == 0

    asyncio.run(exercise())


def test_explicit_global_generation_limit_stops_thirteenth_before_upstream_post() -> None:
    store = GenerationBudgetMemoryStore()
    upstream = CountingSuccessUpstream()
    app = create_relay_app(
        replace(_settings(), max_total_generations=12),
        store=store,
        upstream=upstream,
        start_dispatcher=False,
    )

    with TestClient(app) as client:
        for index in range(12):
            dispatch_id = _dispatch_id(index)
            accepted = client.put(
                f"{DISPATCH_PATH}{dispatch_id}",
                content=_request_body(dispatch_id),
                headers=_headers(),
            )
            assert accepted.status_code == 202
            assert asyncio.run(app.state.relay_dispatcher.run_once()) is True

        thirteenth_id = _dispatch_id(12)
        refused = client.put(
            f"{DISPATCH_PATH}{thirteenth_id}",
            content=_request_body(thirteenth_id),
            headers=_headers(),
        )
        assert refused.status_code == 429
        assert refused.json() == {
            "schema_version": "1.0.0",
            "code": "GENERATION_LIMIT_EXCEEDED",
        }

        # Defense in depth: even a pre-existing uncapped pending row cannot
        # cross the final claim boundary after twelve durable generations.
        store.force_put(_put_request(thirteenth_id))
        with pytest.raises(RelayGenerationLimitExceeded, match="before Provider POST"):
            asyncio.run(app.state.relay_dispatcher.run_once())

    assert upstream.posts == 12
    assert store.total_generations == 12
    assert store.resources[thirteenth_id].generation_count == 0
    assert store.observed_limits == {12}


def test_terminal_resource_survives_client_ack_loss_and_relay_restart_get() -> None:
    store = MemoryRelayStore()
    first = create_relay_app(
        _settings(),
        store=store,
        upstream=SuccessUpstream(),
        start_dispatcher=False,
    )
    headers = _headers()
    with TestClient(first) as client:
        response = client.put(f"{DISPATCH_PATH}{DISPATCH_ID}", content=_request_body(), headers=headers)
        assert response.status_code == 202
        assert asyncio.run(first.state.relay_dispatcher.run_once()) is True
        # Simulate losing the terminal PUT acknowledgement: do not consume one;
        # construct a fresh app/process using only the persisted store.

    restarted = create_relay_app(
        _settings(),
        store=store,
        upstream=SuccessUpstream(),
        start_dispatcher=False,
    )
    with TestClient(restarted) as client:
        recovered = client.get(f"{DISPATCH_PATH}{DISPATCH_ID}", headers=headers)
        assert recovered.status_code == 200
        assert recovered.json()["state"] == "SUCCEEDED"
        assert recovered.json()["generation_count"] == 1
        assert recovered.json()["provider_response"]["body_sha256"] == hashlib.sha256(
            b'{"ok":true}'
        ).hexdigest()
        replay = client.put(f"{DISPATCH_PATH}{DISPATCH_ID}", content=_request_body(), headers=headers)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True


def test_private_http_auth_host_closed_schema_conflict_and_expiry() -> None:
    store = MemoryRelayStore()
    app = create_relay_app(
        _settings(),
        store=store,
        upstream=SuccessUpstream(),
        start_dispatcher=False,
    )
    with TestClient(app) as client:
        assert client.get("/v1/llm/capabilities").status_code == 400
        assert (
            client.get(
                "/v1/llm/capabilities",
                headers={"x-yaya-llm-protocol": PROTOCOL},
            ).status_code
            == 401
        )
        assert client.get("/v1/llm/capabilities", headers=_headers()).status_code == 200
        assert client.get("/not-public", headers=_headers()).status_code == 404
        assert (
            client.get(
                "/v1/llm/capabilities",
                headers={**_headers(), "host": "public.example"},
            ).status_code
            == 421
        )
        assert client.put(
            f"{DISPATCH_PATH}{DISPATCH_ID}",
            content=_request_body(),
            headers=_headers(),
        ).status_code == 202

        changed = json.loads(_request_body())
        changed["request_sha256"] = "f" * 64
        conflict = client.put(
            f"{DISPATCH_PATH}{DISPATCH_ID}",
            content=canonical_response_bytes(changed),
            headers=_headers(),
        )
        assert conflict.status_code == 409

        store.expired = True
        assert client.get(f"{DISPATCH_PATH}{DISPATCH_ID}", headers=_headers()).status_code == 410


class MemoryRelayStore(RelayStore):
    def __init__(self) -> None:
        self.resource: RelayResource | None = None
        self.expired = False

    async def put(
        self,
        request: RelayPutRequest,
        *,
        max_total_generations: int | None = None,
    ) -> StorePutResult:
        del max_total_generations
        if self.expired:
            raise RelayDispatchExpired("expired")
        if self.resource is not None:
            if (
                self.resource.request_body_sha256 != request.body_sha256
                or self.resource.request_sha256 != request.request_sha256
                or self.resource.context_sha256 != request.context_sha256
                or self.resource.completion_sha256 != request.completion_sha256
            ):
                raise RelayDispatchConflict("conflict")
            return StorePutResult(self.resource, False)
        now = datetime.now(UTC)
        self.resource = RelayResource(
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
        return StorePutResult(self.resource, True)

    async def get(self, dispatch_id: str) -> RelayResource | None:
        if self.expired:
            raise RelayDispatchExpired("expired")
        if self.resource is None or self.resource.dispatch_id != dispatch_id:
            return None
        return self.resource

    async def claim_next(
        self,
        upstream_deadline_seconds: float,
        *,
        max_total_generations: int | None = None,
    ) -> RelayResource | None:
        del max_total_generations
        if self.resource is None or self.resource.generation_count != 0:
            return None
        now = datetime.now(UTC)
        self.resource = replace(
            self.resource,
            generation_count=1,
            dispatch_started_at=now,
            upstream_deadline_at=now + timedelta(seconds=upstream_deadline_seconds),
            updated_at=now,
        )
        return self.resource

    async def complete_response(
        self,
        claim: RelayResource,
        response: ProviderHttpResponse,
    ) -> RelayResource:
        now = datetime.now(UTC)
        self.resource = replace(
            claim,
            state="SUCCEEDED",
            response_http_status=response.status,
            response_content_type=response.content_type,
            response_body=response.body,
            response_body_sha256=response.body_sha256,
            terminal_at=now,
            expires_at=now + timedelta(days=7),
            updated_at=now,
        )
        return self.resource

    async def complete_failure(
        self,
        claim: RelayResource,
        *,
        code: str,
        retryable: bool,
    ) -> RelayResource:
        now = datetime.now(UTC)
        self.resource = replace(
            claim,
            state="FAILED",
            failure_code=code,
            failure_retryable=retryable,
            terminal_at=now,
            expires_at=now + timedelta(days=7),
            updated_at=now,
        )
        return self.resource

    async def recover_acknowledgement_unknown(self) -> int:
        resource = self.resource
        if (
            resource is None
            or resource.state != "PENDING"
            or resource.generation_count != 1
            or resource.upstream_deadline_at is None
            or resource.upstream_deadline_at > datetime.now(UTC)
        ):
            return 0
        await self.complete_failure(
            resource,
            code="UPSTREAM_ACKNOWLEDGEMENT_UNKNOWN",
            retryable=False,
        )
        return 1

    async def scrub_expired(self) -> int:
        return 0

    async def statistics(self) -> dict[str, object]:
        resource = self.resource
        return {
            "schema_version": "1.0.0",
            "protocol": PROTOCOL,
            "unique_dispatches": 0 if resource is None else 1,
            "total_generations": 0 if resource is None else resource.generation_count,
            "max_generation_count": 0 if resource is None else resource.generation_count,
            "states": {} if resource is None else {resource.state: 1},
            "dispatches": [],
        }


class GenerationBudgetMemoryStore(RelayStore):
    def __init__(self) -> None:
        self.resources: dict[str, RelayResource] = {}
        self.observed_limits: set[int] = set()

    @property
    def total_generations(self) -> int:
        return sum(resource.generation_count for resource in self.resources.values())

    async def put(
        self,
        request: RelayPutRequest,
        *,
        max_total_generations: int | None = None,
    ) -> StorePutResult:
        existing = self.resources.get(request.dispatch_id)
        if existing is not None:
            if existing.request_body_sha256 != request.body_sha256:
                raise RelayDispatchConflict("conflict")
            return StorePutResult(existing, False)
        if max_total_generations is not None:
            self.observed_limits.add(max_total_generations)
            if len(self.resources) >= max_total_generations:
                raise RelayGenerationLimitExceeded(
                    "global upstream generation limit exhausted before Provider POST"
                )
        resource = _pending_resource(request)
        self.resources[request.dispatch_id] = resource
        return StorePutResult(resource, True)

    def force_put(self, request: RelayPutRequest) -> None:
        self.resources[request.dispatch_id] = _pending_resource(request)

    async def get(self, dispatch_id: str) -> RelayResource | None:
        return self.resources.get(dispatch_id)

    async def claim_next(
        self,
        upstream_deadline_seconds: float,
        *,
        max_total_generations: int | None = None,
    ) -> RelayResource | None:
        pending = next(
            (
                resource
                for _, resource in sorted(self.resources.items())
                if resource.state == "PENDING" and resource.generation_count == 0
            ),
            None,
        )
        if pending is None:
            return None
        if max_total_generations is not None:
            self.observed_limits.add(max_total_generations)
            if self.total_generations >= max_total_generations:
                raise RelayGenerationLimitExceeded(
                    "global upstream generation limit exhausted before Provider POST"
                )
        now = datetime.now(UTC)
        claimed = replace(
            pending,
            generation_count=1,
            dispatch_started_at=now,
            upstream_deadline_at=now + timedelta(seconds=upstream_deadline_seconds),
            updated_at=now,
        )
        self.resources[claimed.dispatch_id] = claimed
        return claimed

    async def complete_response(
        self,
        claim: RelayResource,
        response: ProviderHttpResponse,
    ) -> RelayResource:
        now = datetime.now(UTC)
        completed = replace(
            claim,
            state="SUCCEEDED",
            response_http_status=response.status,
            response_content_type=response.content_type,
            response_body=response.body,
            response_body_sha256=response.body_sha256,
            terminal_at=now,
            expires_at=now + timedelta(days=7),
            updated_at=now,
        )
        self.resources[claim.dispatch_id] = completed
        return completed

    async def complete_failure(
        self,
        claim: RelayResource,
        *,
        code: str,
        retryable: bool,
    ) -> RelayResource:
        del code, retryable
        return claim

    async def recover_acknowledgement_unknown(self) -> int:
        return 0

    async def scrub_expired(self) -> int:
        return 0

    async def statistics(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "protocol": PROTOCOL,
            "unique_dispatches": len(self.resources),
            "total_generations": self.total_generations,
            "max_generation_count": max(
                (resource.generation_count for resource in self.resources.values()),
                default=0,
            ),
            "states": {},
            "dispatches": [],
        }


class UnknownAckUpstream:
    def __init__(self) -> None:
        self.posts = 0

    async def post_completion(self, completion: object) -> ProviderHttpResponse:
        del completion
        self.posts += 1
        raise UpstreamAcknowledgementUnknown("unknown")


class SuccessUpstream:
    async def post_completion(self, completion: object) -> ProviderHttpResponse:
        del completion
        return ProviderHttpResponse(200, "application/json", b'{"ok":true}')


class CountingSuccessUpstream:
    def __init__(self) -> None:
        self.posts = 0

    async def post_completion(self, completion: object) -> ProviderHttpResponse:
        del completion
        self.posts += 1
        return ProviderHttpResponse(200, "application/json", b'{"ok":true}')


class OneShotMaintenanceFailureStore(GenerationBudgetMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.maintenance_calls = 0

    async def scrub_expired(self) -> int:
        self.maintenance_calls += 1
        if self.maintenance_calls == 1:
            raise ConnectionError("synthetic transient store failure")
        return 0


class OneShotCompletionFailureStore(MemoryRelayStore):
    def __init__(self) -> None:
        super().__init__()
        self.completion_calls = 0

    async def complete_response(
        self,
        claim: RelayResource,
        response: ProviderHttpResponse,
    ) -> RelayResource:
        self.completion_calls += 1
        if self.completion_calls == 1:
            raise ConnectionError("synthetic ambiguous completion store failure")
        return await super().complete_response(claim, response)


def _settings() -> RelaySettings:
    return RelaySettings(
        database_url="postgresql+asyncpg://walnut:unused@postgres/walnut",
        relay_api_key=RELAY_KEY,
        upstream_api_key=UPSTREAM_KEY,
        provider=PROVIDER,
        model=MODEL,
        upstream_endpoint="https://api.deepseek.com/chat/completions",
        result_retention_seconds=604_800,
        max_request_bytes=8192,
        max_response_bytes=8192,
        upstream_timeout_ms=100,
        acknowledgement_grace_seconds=0,
    )


def _settings_env() -> dict[str, str]:
    return {
        "WALNUT_DATABASE_URL": "postgresql+asyncpg://walnut:unused@postgres/walnut",
        "WALNUT_LLM_RELAY_SERVER_API_KEY": RELAY_KEY,
        "WALNUT_LLM_UPSTREAM_API_KEY": UPSTREAM_KEY,
        "WALNUT_LLM_PROVIDER": PROVIDER,
        "WALNUT_LLM_MODEL": MODEL,
        "WALNUT_LLM_UPSTREAM_ENDPOINT": "https://api.deepseek.com/chat/completions",
        "WALNUT_LLM_RELAY_MAX_REQUEST_BYTES": "8192",
        "WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES": "8192",
        "WALNUT_LLM_UPSTREAM_TIMEOUT_MS": "100",
        "WALNUT_LLM_ACKNOWLEDGEMENT_GRACE_SECONDS": "0",
    }


def _harden_test_secret_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    _run_acl_test_script(
        path,
        r"""
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
& icacls.exe $env:WALNUT_TEST_SECRET_PATH `
    /inheritance:r `
    /grant:r "*$($currentSid.Value):(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'icacls failed to harden test secret' }
""",
    )


def _grant_broad_test_read(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o644)
        return
    completed = subprocess.run(
        ["icacls.exe", str(path), "/grant", "*S-1-5-32-545:(R)"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr


def _set_null_test_dacl(path: Path) -> None:
    _run_acl_test_script(
        path,
        r"""
$acl = Get-Acl -LiteralPath $env:WALNUT_TEST_SECRET_PATH -ErrorAction Stop
$acl.SetSecurityDescriptorSddlForm(
    'D:NO_ACCESS_CONTROL',
    [System.Security.AccessControl.AccessControlSections]::Access
)
Set-Acl -LiteralPath $env:WALNUT_TEST_SECRET_PATH -AclObject $acl -ErrorAction Stop
""",
    )


def _run_acl_test_script(path: Path, script: str) -> None:
    environment = dict(os.environ)
    environment["WALNUT_TEST_SECRET_PATH"] = str(path)
    system_root = environment.get("SystemRoot") or environment.get("WINDIR", r"C:\Windows")
    windows_powershell_modules = str(
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
    )
    inherited_module_path = environment.get("PSModulePath", "")
    environment["PSModulePath"] = os.pathsep.join(
        [windows_powershell_modules]
        + [
            entry
            for entry in inherited_module_path.split(os.pathsep)
            if entry and entry.casefold() != windows_powershell_modules.casefold()
        ]
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop';" + script,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr


def _headers() -> dict[str, str]:
    return {
        "authorization": f"Bearer {RELAY_KEY}",
        "x-yaya-llm-protocol": PROTOCOL,
        "content-type": "application/json; charset=utf-8",
    }


def _put_request(dispatch_id: str = DISPATCH_ID) -> RelayPutRequest:
    return parse_put_request(
        dispatch_id,
        _request_body(dispatch_id),
        provider=PROVIDER,
        model=MODEL,
        maximum_bytes=8192,
    )


def _request_body(dispatch_id: str = DISPATCH_ID) -> bytes:
    completion: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "diagnostic"}],
        "temperature": 0,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    from yaya_agent_runtime import llm_recovery_sha256

    value: dict[str, object] = {
        "schema_version": "1.0.0",
        "dispatch_id": dispatch_id,
        "request_sha256": "1" * 64,
        "context_sha256": "2" * 64,
        "completion_sha256": llm_recovery_sha256(
            {
                "schema_version": "1.0.0",
                "provider": PROVIDER,
                "model": MODEL,
                "completion": completion,
            }
        ),
        "provider": PROVIDER,
        "model": MODEL,
        "completion": completion,
    }
    return canonical_response_bytes(value)


def _dispatch_id(index: int) -> str:
    return f"llmdsp_{index:040x}"


def _pending_resource(request: RelayPutRequest) -> RelayResource:
    now = datetime.now(UTC)
    return RelayResource(
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
