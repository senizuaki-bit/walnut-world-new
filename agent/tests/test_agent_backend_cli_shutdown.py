from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from postgres_test_support import PostgresTestServer, postgres_test_server  # noqa: E402
from yaya_agent_backend.__main__ import _serve  # noqa: E402
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.config import ProductionSettings  # noqa: E402
from yaya_agent_backend.http_api import (  # noqa: E402
    AgentHttpApi,
    ThreadingHTTPServer,
)
from yaya_agent_backend.http_router import ProductionHttpApi  # noqa: E402
from yaya_agent_backend.product_http_api import ProductHttpApi  # noqa: E402
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402

PINNED_SANDBOX_IMAGE = "gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c"
_STARTUP_TIMEOUT_SECONDS = 45.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0

_CANCELLABLE_PRODUCTION_RUNNER = r"""
import asyncio
import os
import sys
from pathlib import Path

import yaya_agent_backend.__main__ as production_cli
from yaya_agent_backend.application import AgentTurnWorker
from yaya_agent_backend.config import LearnerWorkerSettings, ProductionSettings
from yaya_agent_backend.learner_projection import LearnerProjectionWorker


async def main() -> None:
    role = sys.argv[1]
    ready_path = Path(os.environ["YAYA_TEST_RUNNER_READY"])
    if role == "worker":
        original_run_forever = AgentTurnWorker.run_forever

        async def observed_run_forever(self, stop):
            ready_path.write_text("worker-ready", encoding="utf-8")
            return await original_run_forever(self, stop)

        AgentTurnWorker.run_forever = observed_run_forever
        action = production_cli._worker
    elif role == "learner-worker":
        original_run_forever = LearnerProjectionWorker.run_forever

        async def observed_run_forever(self, stop):
            ready_path.write_text("learner-worker-ready", encoding="utf-8")
            return await original_run_forever(self, stop)

        LearnerProjectionWorker.run_forever = observed_run_forever
        action = production_cli._learner_worker
    elif role == "serve":
        action = production_cli._serve
    else:
        raise RuntimeError(f"unsupported production role {role}")

    settings = (
        LearnerWorkerSettings.from_env()
        if role == "learner-worker"
        else ProductionSettings.from_env()
    )
    service = asyncio.create_task(action(settings))
    cancel_input = asyncio.create_task(asyncio.to_thread(sys.stdin.readline))
    done, _ = await asyncio.wait(
        {service, cancel_input},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if service in done:
        cancel_input.cancel()
        await asyncio.gather(cancel_input, return_exceptions=True)
        await service
        raise RuntimeError("production service returned before cancellation")
    if cancel_input.result().strip() != "cancel":
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
        raise RuntimeError("test controller closed without a cancellation request")
    service.cancel()
    try:
        await service
    except asyncio.CancelledError:
        pass
    print("RUNNER_CANCELLED", flush=True)


asyncio.run(main())
print("RUNNER_EXITED", flush=True)
"""


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    if not 1 <= port <= 65_535:
        raise AssertionError(f"kernel selected invalid HTTP port {port}")
    return port


def _can_bind_exclusively(port: int) -> tuple[bool, str | None]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name != "nt":
            # Match ThreadingHTTPServer's POSIX rebinding semantics so a closed
            # accepted connection in TIME_WAIT is not mistaken for a live listener.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        return True, None
    except OSError as error:
        return False, f"{type(error).__name__}: {error}"
    finally:
        listener.close()


def _exchange_loopback(port: int, request: bytes) -> tuple[int, dict[str, str], bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.settimeout(10)
        connection.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                raise AssertionError("HTTP server closed before sending response headers")
            response.extend(chunk)
        raw_headers, body_prefix = bytes(response).split(b"\r\n\r\n", 1)
        lines = raw_headers.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        content_length = int(headers["content-length"])
        body = bytearray(body_prefix)
        while len(body) < content_length:
            chunk = connection.recv(content_length - len(body))
            if not chunk:
                raise AssertionError("HTTP server closed before sending response body")
            body.extend(chunk)
        return status, headers, bytes(body[:content_length])


class AgentBackendServeWiringTests(unittest.TestCase):
    def _assert_http_server_error_policy(self) -> None:
        server = object.__new__(ThreadingHTTPServer)
        request = object()
        client_address = ("127.0.0.1", 18_080)

        expected_disconnect_output = io.StringIO()
        with contextlib.redirect_stderr(expected_disconnect_output):
            for error in (
                ConnectionResetError(10054, "client reset"),
                ConnectionAbortedError(10053, "client aborted"),
                BrokenPipeError(32, "client closed response stream"),
            ):
                try:
                    raise error
                except OSError:
                    server.handle_error(request, client_address)
        self.assertEqual(expected_disconnect_output.getvalue(), "")

        unexpected_error_output = io.StringIO()
        with contextlib.redirect_stderr(unexpected_error_output):
            try:
                raise RuntimeError("unexpected-handler-failure")
            except RuntimeError:
                server.handle_error(request, client_address)
        self.assertIn("Traceback", unexpected_error_output.getvalue())
        self.assertIn("unexpected-handler-failure", unexpected_error_output.getvalue())

    def test_serve_passes_exact_game_product_composite_to_http_server(self) -> None:
        game_application = object()
        product_application = object()
        student_chain_application = object()
        draft_application = object()
        authenticator = JwtAuthenticator(
            hmac_secret="serve-wiring-secret-" + "s" * 48,
            issuer="yaya-serve-wiring-test",
            audience="yaya-serve-wiring-api",
        )
        validator = ContractSchemaValidator(CONTRACTS_ROOT)
        composition = SimpleNamespace(
            application=game_application,
            product_application=product_application,
            student_chain_application=student_chain_application,
            draft_application=draft_application,
            authenticator=authenticator,
            validator=validator,
        )
        settings = cast(
            ProductionSettings,
            SimpleNamespace(http_host="127.0.0.1", http_port=18_080),
        )
        captured: list[tuple[object, str, int]] = []

        class FakeServer:
            def __init__(self) -> None:
                self.shutdown_calls = 0

            def shutdown(self) -> None:
                self.shutdown_calls += 1

        fake_server = FakeServer()

        def fake_serve_http(
            api: object,
            host: str,
            port: int,
            **kwargs: object,
        ) -> None:
            captured.append((api, host, port))
            server_created = kwargs["server_created"]
            if not callable(server_created):
                raise AssertionError("_serve did not supply a server-created callback")
            server_created(fake_server)

        composition_factory = AsyncMock(return_value=composition)
        with (
            patch(
                "yaya_agent_backend.__main__.create_production_composition",
                composition_factory,
            ),
            patch(
                "yaya_agent_backend.__main__.serve_http",
                side_effect=fake_serve_http,
            ) as serve_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped unexpectedly"):
                asyncio.run(_serve(settings))

        composition_factory.assert_awaited_once_with(settings)
        self.assertEqual(serve_mock.call_count, 1)
        self.assertEqual(len(captured), 1)
        composite, host, port = captured[0]
        self.assertIs(type(composite), ProductionHttpApi)
        self.assertEqual((host, port), ("127.0.0.1", 18_080))
        game = getattr(composite, "_game")
        product = getattr(composite, "_product")
        self.assertIs(type(game), AgentHttpApi)
        self.assertIs(type(product), ProductHttpApi)
        self.assertIs(getattr(game, "_application"), game_application)
        self.assertIs(getattr(product, "_application"), product_application)
        self.assertIs(getattr(game, "_student_chain"), student_chain_application)
        self.assertIs(getattr(product, "_draft_application"), draft_application)
        self.assertIs(getattr(game, "_authenticator"), authenticator)
        self.assertIs(getattr(product, "_authenticator"), authenticator)
        self.assertIs(getattr(game, "_validator"), validator)
        self.assertIs(getattr(product, "_validator"), validator)
        self.assertEqual(fake_server.shutdown_calls, 1)
        self._assert_http_server_error_policy()


class AgentBackendCliShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._postgres_context = postgres_test_server()
        cls._artifacts_context = tempfile.TemporaryDirectory(prefix="yaya-cli-shutdown-")
        try:
            cls.server: PostgresTestServer = cls._postgres_context.__enter__()
            cls.artifact_root = Path(cls._artifacts_context.__enter__()).resolve()
        except BaseException:
            cls._artifacts_context.__exit__(*sys.exc_info())
            cls._postgres_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._artifacts_context.__exit__(None, None, None)
        cls._postgres_context.__exit__(None, None, None)

    def _environment(self, *, http_port: int) -> dict[str, str]:
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH", "").strip()
        environment["PYTHONPATH"] = str(PACKAGE_ROOT) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        environment.update(
            {
                "PYTHONUNBUFFERED": "1",
                "YAYA_DATABASE_DSN": self.server.dsn,
                "YAYA_ARTIFACT_ROOT": str(self.artifact_root),
                "YAYA_CONTRACTS_ROOT": str(CONTRACTS_ROOT),
                "YAYA_AUTH_HMAC_SECRET": "cli-shutdown-secret-" + "s" * 48,
                "YAYA_AUTH_ISSUER": "yaya-cli-shutdown-test",
                "YAYA_AUTH_AUDIENCE": "yaya-cli-shutdown-api",
                "YAYA_LLM_MODE": "fallback",
                "YAYA_LLM_MODEL": "explicit-fallback",
                "YAYA_LLM_PROVIDER": "explicit-fallback",
                "YAYA_HTTP_HOST": "127.0.0.1",
                "YAYA_HTTP_PORT": str(http_port),
                "YAYA_WORKER_ID": "worker_cli_shutdown_0001",
                "YAYA_WORKER_LEASE_SECONDS": "2",
                "YAYA_WORKER_POLL_MS": "10",
                "YAYA_LEARNER_WORKER_ID": "learner_worker_cli_shutdown_0001",
                "YAYA_LEARNER_WORKER_LEASE_SECONDS": "2",
                "YAYA_LEARNER_WORKER_POLL_MS": "10",
                "YAYA_SANDBOX_IMAGE": PINNED_SANDBOX_IMAGE,
                "YAYA_DOCKER_EXE": "docker",
            }
        )
        # A machine-level provider secret must never silently change this
        # regression from explicit fallback mode into an external dependency.
        for name in (
            "YAYA_LLM_ENDPOINT",
            "YAYA_LLM_API_KEY",
            "YAYA_LLM_API_KEY_FILE",
            "YAYA_LLM_THINKING_MODE",
        ):
            environment.pop(name, None)
        return environment

    def _spawn(
        self,
        command: str,
        *,
        http_port: int,
    ) -> tuple[subprocess.Popen[str], Path]:
        environment = self._environment(http_port=http_port)
        if command == "learner-worker":
            for name in (
                "YAYA_ARTIFACT_ROOT",
                "YAYA_AUTH_HMAC_SECRET",
                "YAYA_AUTH_ISSUER",
                "YAYA_AUTH_AUDIENCE",
                "YAYA_LLM_MODE",
                "YAYA_LLM_MODEL",
                "YAYA_LLM_PROVIDER",
                "YAYA_HTTP_HOST",
                "YAYA_HTTP_PORT",
                "YAYA_WORKER_ID",
                "YAYA_WORKER_LEASE_SECONDS",
                "YAYA_WORKER_POLL_MS",
                "YAYA_SANDBOX_IMAGE",
                "YAYA_DOCKER_EXE",
            ):
                environment.pop(name, None)
        ready_path = self.artifact_root / f"{command}-{uuid.uuid4().hex}.ready"
        environment["YAYA_TEST_RUNNER_READY"] = str(ready_path)
        process = subprocess.Popen(
            [sys.executable, "-c", _CANCELLABLE_PRODUCTION_RUNNER, command],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return process, ready_path

    def _spawn_cli(self, command: str, *, http_port: int) -> subprocess.Popen[str]:
        options: dict[str, object] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        return subprocess.Popen(
            [sys.executable, "-m", "yaya_agent_backend", command],
            cwd=REPOSITORY_ROOT,
            env=self._environment(http_port=http_port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **options,
        )

    def _cancel(self, process: subprocess.Popen[str]) -> None:
        if process.stdin is None:
            self.fail("cancellable production runner has no control pipe")
        process.stdin.write("cancel\n")
        process.stdin.flush()

    def _interrupt_cli(self, process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)

    def _cleanup(self, process: subprocess.Popen[str]) -> tuple[str, str]:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=10)
        return stdout, stderr

    def _wait_http_ready(self, process: subprocess.Popen[str], port: int) -> None:
        request = (
            b"GET /v1/commands/cmd_cli_shutdown_0001 HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Request-Id: req_cli_shutdown_0001\r\n"
            b"X-Trace-Id: trace_cli_shutdown_0001\r\n"
            b"X-Correlation-Id: corr_cli_shutdown_0001\r\n"
            b"Connection: close\r\n\r\n"
        )
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(
                    f"serve exited before HTTP readiness with {return_code}; "
                    f"stdout={stdout[-1000:]!r}; stderr={stderr[-1000:]!r}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5) as client:
                    client.settimeout(1.0)
                    client.sendall(request)
                    response = client.recv(4096)
                if response.startswith(b"HTTP/"):
                    return
            except (ConnectionError, OSError, TimeoutError):
                pass
            time.sleep(0.05)
        self.fail(f"serve did not become HTTP-ready within {_STARTUP_TIMEOUT_SECONDS:.0f}s")

    def _assert_product_prefix_reaches_product_adapter(self, port: int) -> None:
        request = (
            b"GET /product-experience/v1/sessions/session_cli_product_0001/"
            b"agent-interactions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Schema-Version: 1.0.0\r\n"
            b"X-Request-Id: req_cli_product_0001\r\n"
            b"X-Trace-Id: trace_cli_product_0001\r\n"
            b"X-Correlation-Id: corr_cli_product_0001\r\n"
            b"Connection: close\r\n\r\n"
        )
        status, headers, body = _exchange_loopback(port, request)
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(payload["error"]["stage"], "PRODUCT_VALIDATE")
        self.assertEqual(headers["x-request-id"], "req_cli_product_0001")
        self.assertEqual(headers["x-trace-id"], "trace_cli_product_0001")
        self.assertEqual(headers["x-correlation-id"], "corr_cli_product_0001")

    def _wait_worker_ready(
        self,
        process: subprocess.Popen[str],
        ready_path: Path,
        *,
        expected: str = "worker-ready",
    ) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(
                    f"worker exited during composition with {return_code}; "
                    f"stdout={stdout[-1000:]!r}; stderr={stderr[-1000:]!r}"
                )
            if ready_path.is_file():
                self.assertEqual(ready_path.read_text(encoding="utf-8"), expected)
                return
            time.sleep(0.05)
        self.fail(f"worker did not become cancellable within {_STARTUP_TIMEOUT_SECONDS:.0f}s")

    def test_serve_cancellation_closes_http_thread_and_releases_port(self) -> None:
        port = _reserve_loopback_port()
        process, _ = self._spawn("serve", http_port=port)
        stdout = ""
        stderr = ""
        try:
            self._wait_http_ready(process, port)
            started = time.monotonic()
            self._cancel(process)
            try:
                return_code = process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                elapsed = time.monotonic() - started
                rebound, bind_error = _can_bind_exclusively(port)
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - started
                rebound, bind_error = _can_bind_exclusively(port)
                self.fail(
                    "serve ignored bounded cancellation: asyncio.to_thread kept "
                    f"serve_forever alive for {elapsed:.3f}s; "
                    f"port_rebound={rebound}; bind_error={bind_error!r}"
                )
            self.assertEqual(return_code, 0)
            self.assertLess(elapsed, _SHUTDOWN_TIMEOUT_SECONDS)
            self.assertTrue(
                rebound,
                f"HTTPServer.server_close did not release port {port}: {bind_error}",
            )
        finally:
            stdout, stderr = self._cleanup(process)
        self.assertIn("RUNNER_CANCELLED", stdout)
        self.assertIn("RUNNER_EXITED", stdout)
        self.assertNotIn("Traceback", stdout + stderr)

    def test_serve_cli_interrupt_runs_cleanup_instead_of_hard_exit(self) -> None:
        port = _reserve_loopback_port()
        process = self._spawn_cli("serve", http_port=port)
        stdout = ""
        stderr = ""
        try:
            self._wait_http_ready(process, port)
            self._assert_product_prefix_reaches_product_adapter(port)
            started = time.monotonic()
            self._interrupt_cli(process)
            try:
                return_code = process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                rebound, bind_error = _can_bind_exclusively(port)
                self.fail(
                    "production CLI did not finish graceful HTTP shutdown within "
                    f"{_SHUTDOWN_TIMEOUT_SECONDS:.0f}s; port_rebound={rebound}; "
                    f"bind_error={bind_error!r}"
                )
            elapsed = time.monotonic() - started
            rebound, bind_error = _can_bind_exclusively(port)
            self.assertEqual(return_code, 130)
            self.assertLess(elapsed, _SHUTDOWN_TIMEOUT_SECONDS)
            self.assertTrue(
                rebound,
                f"CLI exit did not run HTTPServer.server_close for port {port}: {bind_error}",
            )
        finally:
            stdout, stderr = self._cleanup(process)
        self.assertNotIn("Traceback", stdout + stderr)

    def test_worker_cancellation_stops_run_forever_within_bound(self) -> None:
        process, ready_path = self._spawn("worker", http_port=_reserve_loopback_port())
        stdout = ""
        stderr = ""
        try:
            self._wait_worker_ready(process, ready_path)
            started = time.monotonic()
            self._cancel(process)
            try:
                return_code = process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.fail(
                    f"worker run_forever ignored cancellation for {time.monotonic() - started:.3f}s"
                )
            elapsed = time.monotonic() - started
            self.assertEqual(return_code, 0)
            self.assertLess(elapsed, _SHUTDOWN_TIMEOUT_SECONDS)
        finally:
            stdout, stderr = self._cleanup(process)
        self.assertIn("RUNNER_CANCELLED", stdout)
        self.assertIn("RUNNER_EXITED", stdout)
        self.assertNotIn("Traceback", stdout + stderr)

    def test_learner_worker_cancellation_stops_run_forever_within_bound(self) -> None:
        process, ready_path = self._spawn(
            "learner-worker",
            http_port=_reserve_loopback_port(),
        )
        stdout = ""
        stderr = ""
        try:
            self._wait_worker_ready(
                process,
                ready_path,
                expected="learner-worker-ready",
            )
            started = time.monotonic()
            self._cancel(process)
            try:
                return_code = process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.fail(
                    "learner worker run_forever ignored cancellation for "
                    f"{time.monotonic() - started:.3f}s"
                )
            elapsed = time.monotonic() - started
            self.assertEqual(return_code, 0)
            self.assertLess(elapsed, _SHUTDOWN_TIMEOUT_SECONDS)
        finally:
            stdout, stderr = self._cleanup(process)
        self.assertIn("RUNNER_CANCELLED", stdout)
        self.assertIn("RUNNER_EXITED", stdout)
        self.assertNotIn("Traceback", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
