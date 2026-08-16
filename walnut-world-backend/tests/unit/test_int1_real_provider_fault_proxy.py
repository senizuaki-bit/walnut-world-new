from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.client import HTTPConnection, RemoteDisconnected
from pathlib import Path
from typing import NamedTuple, cast

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROXY_PATH = BACKEND_ROOT / "scripts" / "int1_real_provider_fault_proxy.py"
SPEC = importlib.util.spec_from_file_location("int1_real_provider_fault_proxy", PROXY_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery invariant
    raise RuntimeError("could not load the INT1 real-Provider fault proxy")
proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proxy
SPEC.loader.exec_module(proxy)

API_KEY = "relay-secret-only"
DISPATCH_ID = "llmdsp_" + "a" * 40
SECOND_DISPATCH_ID = "llmdsp_" + "b" * 40


class _HttpResponse(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


def _resource(dispatch_id: str, state: str, generation_count: int, status: int) -> object:
    return proxy.ProxyResponse(
        status,
        {"content-type": "application/json"},
        json.dumps(
            {
                "schema_version": "1.0.0",
                "dispatch_id": dispatch_id,
                "state": state,
                "generation_count": generation_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


class _ScriptedTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.first_dispatch_polls = 0

    def __call__(self, method: str, path: str, body: bytes | None) -> object:
        self.calls.append((method, path, body))
        dispatch_id = path.removeprefix(proxy.DISPATCH_PATH)
        if method == "PUT" and dispatch_id == DISPATCH_ID:
            return _resource(DISPATCH_ID, "PENDING", 0, 202)
        if method == "GET" and dispatch_id == DISPATCH_ID:
            self.first_dispatch_polls += 1
            if self.first_dispatch_polls == 1:
                return _resource(DISPATCH_ID, "PENDING", 1, 202)
            return _resource(DISPATCH_ID, "SUCCEEDED", 1, 200)
        if method == "PUT" and dispatch_id == SECOND_DISPATCH_ID:
            return _resource(SECOND_DISPATCH_ID, "PENDING", 0, 202)
        raise AssertionError((method, path, body))


def test_first_put_waits_for_terminal_then_drops_and_get_recovers_same_generation() -> None:
    transport = _ScriptedTransport()
    state = proxy.FaultProxyState(
        API_KEY,
        8123,
        terminal_wait_seconds=1,
        poll_seconds=0.001,
        transport=transport,
    )

    response, drop = state.put(DISPATCH_ID, b"{}")

    assert response is None
    assert drop is True
    assert [call[:2] for call in transport.calls] == [
        ("PUT", f"{proxy.DISPATCH_PATH}{DISPATCH_ID}"),
        ("GET", f"{proxy.DISPATCH_PATH}{DISPATCH_ID}"),
        ("GET", f"{proxy.DISPATCH_PATH}{DISPATCH_ID}"),
    ]

    unavailable, forced_unavailable = state.get(DISPATCH_ID)
    assert unavailable.status == 503
    assert forced_unavailable is True
    assert len(transport.calls) == 3
    attempted = state.statistics()
    assert attempted["reconcile_unavailable_attempted"] == 1
    assert attempted["reconcile_unavailable_delivered"] == 0
    state.record_reconcile_unavailable_delivered(DISPATCH_ID)

    recovered, forced_unavailable = state.get(DISPATCH_ID)
    assert recovered.status == 200
    assert forced_unavailable is False
    evidence = state.statistics()
    assert evidence == {
        "schema_version": "1.0.0",
        "classification": proxy.CLASSIFICATION,
        "fault_dispatch_id": DISPATCH_ID,
        "acknowledgement_drops": 1,
        "reconcile_unavailable_attempted": 1,
        "reconcile_unavailable_delivered": 1,
        "terminal_before_drop": True,
        "terminal_state": "SUCCEEDED",
        "terminal_generation_count": 1,
        "recovered_dispatch_id": DISPATCH_ID,
        "recovered_generation_count": 1,
        "recovered_same_dispatch": True,
    }
    serialized = json.dumps(evidence, sort_keys=True)
    assert API_KEY not in serialized
    assert "completion" not in serialized
    assert "provider_response" not in serialized

    second_response, second_drop = state.put(SECOND_DISPATCH_ID, b"{}")
    assert second_response.status == 202
    assert second_drop is False
    assert state.statistics() == evidence


def test_loopback_http_surface_really_closes_put_then_returns_503_and_terminal_get() -> None:
    state = proxy.FaultProxyState(
        API_KEY,
        8123,
        terminal_wait_seconds=1,
        poll_seconds=0.001,
        transport=_ScriptedTransport(),
    )
    server = proxy.FaultProxyServer((proxy.HOST, 0), state)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    port = server.server_address[1]
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "X-Yaya-Llm-Protocol": proxy.PROTOCOL,
    }
    try:
        connection = HTTPConnection(proxy.HOST, port, timeout=2)
        connection.request("PUT", f"{proxy.DISPATCH_PATH}{DISPATCH_ID}", b"{}", headers)
        with pytest.raises(RemoteDisconnected):
            connection.getresponse()
        connection.close()

        unavailable = _http_get(port, f"{proxy.DISPATCH_PATH}{DISPATCH_ID}", headers)
        assert unavailable.status == 503
        recovered = _http_get(port, f"{proxy.DISPATCH_PATH}{DISPATCH_ID}", headers)
        assert recovered.status == 200
        assert json.loads(recovered.body)["generation_count"] == 1
        statistics = _http_get(port, proxy.STATS_PATH, headers)
        evidence = json.loads(statistics.body)
        assert evidence["acknowledgement_drops"] == 1
        assert evidence["reconcile_unavailable_attempted"] == 1
        assert evidence["reconcile_unavailable_delivered"] == 1
        assert evidence["terminal_before_drop"] is True
        assert evidence["recovered_same_dispatch"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


class _FailingResponseWriter:
    def __init__(self, failure: OSError, *, fail_on_flush: bool) -> None:
        self.failure = failure
        self.fail_on_flush = fail_on_flush

    def write(self, body: bytes) -> int:
        if not self.fail_on_flush:
            raise self.failure
        return len(body)

    def flush(self) -> None:
        if self.fail_on_flush:
            raise self.failure


@pytest.mark.parametrize(
    ("failure", "fail_on_flush"),
    [
        (BrokenPipeError("client reset before 503 body"), False),
        (TimeoutError("503 flush timed out"), True),
    ],
)
def test_failed_forced_503_write_or_flush_never_claims_delivery(
    failure: OSError,
    fail_on_flush: bool,
) -> None:
    state = proxy.FaultProxyState(
        API_KEY,
        8123,
        terminal_wait_seconds=1,
        poll_seconds=0.001,
        transport=_ScriptedTransport(),
    )
    assert state.put(DISPATCH_ID, b"{}") == (None, True)
    server = object.__new__(proxy.FaultProxyServer)
    server.state = state
    handler = object.__new__(proxy.FaultProxyHandler)
    handler.server = server
    handler.wfile = _FailingResponseWriter(failure, fail_on_flush=fail_on_flush)
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    with pytest.raises(type(failure), match=str(failure)):
        handler._send_dispatch_get(DISPATCH_ID)

    failed_delivery = state.statistics()
    assert failed_delivery["reconcile_unavailable_attempted"] == 1
    assert failed_delivery["reconcile_unavailable_delivered"] == 0
    recovered, forced_unavailable = state.get(DISPATCH_ID)
    assert recovered.status == 200
    assert forced_unavailable is False
    assert state.statistics()["recovered_same_dispatch"] is True
    assert state.statistics()["reconcile_unavailable_delivered"] == 0


def test_first_arriving_reconciliation_is_reserved_while_put_waits_for_terminal() -> None:
    put_seen = threading.Event()
    terminal_allowed = threading.Event()

    def delayed_terminal(method: str, path: str, body: bytes | None) -> object:
        del body
        dispatch_id = path.removeprefix(proxy.DISPATCH_PATH)
        if method == "PUT":
            put_seen.set()
            return _resource(dispatch_id, "PENDING", 0, 202)
        assert terminal_allowed.wait(1)
        return _resource(dispatch_id, "SUCCEEDED", 1, 200)

    state = proxy.FaultProxyState(
        API_KEY,
        8123,
        terminal_wait_seconds=1,
        poll_seconds=0.001,
        transport=delayed_terminal,
    )
    results: dict[str, object] = {}
    put_thread = threading.Thread(
        target=lambda: results.__setitem__("put", state.put(DISPATCH_ID, b"{}"))
    )
    put_thread.start()
    assert put_seen.wait(1)
    first_get = threading.Thread(
        target=lambda: results.__setitem__("first", state.get(DISPATCH_ID))
    )
    second_get = threading.Thread(
        target=lambda: results.__setitem__("second", state.get(DISPATCH_ID))
    )
    first_get.start()
    reservation_deadline = time.monotonic() + 1
    while not getattr(state, "_first_reconcile_reserved"):
        assert time.monotonic() < reservation_deadline
        time.sleep(0.001)
    second_get.start()
    terminal_allowed.set()
    for thread in (put_thread, first_get, second_get):
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert results["put"] == (None, True)
    first_response, first_forced = cast(tuple[object, bool], results["first"])
    second_response, second_forced = cast(tuple[object, bool], results["second"])
    assert getattr(first_response, "status") == 503
    assert first_forced is True
    assert getattr(second_response, "status") == 200
    assert second_forced is False
    state.record_reconcile_unavailable_delivered(DISPATCH_ID)
    evidence = state.statistics()
    assert evidence["reconcile_unavailable_attempted"] == 1
    assert evidence["reconcile_unavailable_delivered"] == 1
    assert evidence["recovered_same_dispatch"] is True


def test_non_generation_one_terminal_fails_closed_without_claiming_drop() -> None:
    def invalid_terminal(method: str, path: str, body: bytes | None) -> object:
        del method, body
        return _resource(path.removeprefix(proxy.DISPATCH_PATH), "SUCCEEDED", 0, 200)

    state = proxy.FaultProxyState(
        API_KEY,
        8123,
        terminal_wait_seconds=1,
        poll_seconds=0.001,
        transport=invalid_terminal,
    )

    response, drop = state.put(DISPATCH_ID, b"{}")

    assert response.status == 503
    assert drop is False
    evidence = state.statistics()
    assert evidence["acknowledgement_drops"] == 0
    assert evidence["reconcile_unavailable_attempted"] == 0
    assert evidence["reconcile_unavailable_delivered"] == 0
    assert evidence["terminal_before_drop"] is False


def test_proxy_requires_explicit_opt_in_and_rejects_inherited_provider_key() -> None:
    clean = {
        proxy.OPT_IN_ENV: "true",
        proxy.API_KEY_ENV: API_KEY,
    }
    state = proxy.build_state_from_env(8123, clean, transport=_ScriptedTransport())
    assert API_KEY not in repr(state)

    with pytest.raises(ValueError, match="must not inherit"):
        proxy.build_state_from_env(
            8123,
            {**clean, "WALNUT_LLM_UPSTREAM_API_KEY": "provider-secret"},
            transport=_ScriptedTransport(),
        )
    with pytest.raises(ValueError, match="must not inherit"):
        proxy.build_state_from_env(
            8123,
            {**clean, "WALNUT_LLM_UPSTREAM_API_KEY_FILE": "C:/outside/key"},
            transport=_ScriptedTransport(),
        )
    with pytest.raises(ValueError, match="is required"):
        proxy.build_state_from_env(
            8123,
            {proxy.API_KEY_ENV: API_KEY},
            transport=_ScriptedTransport(),
        )


@pytest.mark.parametrize(
    ("protocol_values", "authorization_values", "host_values", "client_is_loopback"),
    [
        ([], [f"Bearer {API_KEY}"], ["127.0.0.1:8123"], True),
        ([proxy.PROTOCOL, proxy.PROTOCOL], [f"Bearer {API_KEY}"], ["localhost"], True),
        ([proxy.PROTOCOL], ["Bearer wrong"], ["127.0.0.1"], True),
        ([proxy.PROTOCOL], [f"Bearer {API_KEY}"], ["example.com"], True),
        ([proxy.PROTOCOL], [f"Bearer {API_KEY}"], ["127.0.0.1"], False),
    ],
)
def test_private_authentication_rejects_ambiguous_or_non_loopback_requests(
    protocol_values: list[str],
    authorization_values: list[str],
    host_values: list[str],
    client_is_loopback: bool,
) -> None:
    assert not proxy.authorized_private_request(
        protocol_values=protocol_values,
        authorization_values=authorization_values,
        host_values=host_values,
        api_key=API_KEY,
        client_is_loopback=client_is_loopback,
    )


def test_private_authentication_and_closed_paths_accept_only_loopback_protocol_surface() -> None:
    assert proxy.authorized_private_request(
        protocol_values=[proxy.PROTOCOL],
        authorization_values=[f"Bearer {API_KEY}"],
        host_values=["127.0.0.1:8123"],
        api_key=API_KEY,
        client_is_loopback=True,
    )
    assert proxy._closed_path(proxy.CAPABILITIES_PATH) == proxy.CAPABILITIES_PATH
    assert proxy._closed_path(proxy.STATS_PATH) == proxy.STATS_PATH
    assert proxy._closed_path(f"{proxy.DISPATCH_PATH}{DISPATCH_ID}") == (
        f"{proxy.DISPATCH_PATH}{DISPATCH_ID}"
    )
    assert proxy._closed_path(f"{proxy.DISPATCH_PATH}{DISPATCH_ID}?leak=true") is None
    assert proxy._closed_path("/v1/chat/completions") is None


def _http_get(port: int, path: str, headers: dict[str, str]) -> _HttpResponse:
    connection = HTTPConnection(proxy.HOST, port, timeout=2)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return _HttpResponse(
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()
