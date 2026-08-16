from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PostgresTestServer:
    dsn: str
    container_name: str


def reset_sandbox_recovery_results(result_root: Path, *, owner_root: Path) -> None:
    """Reset the production Sandbox receipt store paired with a fresh test DB.

    Production keeps these receipts across process restarts.  Tests that truncate
    every business table, however, are creating a new authority universe and must
    not retain receipts for deterministic run ids from the preceding test case.
    """

    resolved_owner_root = owner_root.resolve(strict=True)
    if not resolved_owner_root.is_dir():
        raise AssertionError("sandbox test result owner is not a directory")
    if not result_root.is_absolute():
        raise AssertionError("sandbox test result root must be absolute")
    if result_root.name not in {".sandbox-results", "sandbox-results"}:
        raise AssertionError("sandbox test result root has an unexpected name")
    if result_root.parent.resolve(strict=True) != resolved_owner_root:
        raise AssertionError("sandbox test result root escaped its owner")
    if result_root.is_symlink() or os.path.isjunction(result_root):
        raise AssertionError("sandbox test result root must not be a symbolic link")
    if result_root.exists():
        descendants = list(result_root.rglob("*"))
        if any(
            candidate.is_symlink() or os.path.isjunction(candidate) for candidate in descendants
        ):
            raise AssertionError("sandbox test result root contains a symbolic link")
        for candidate in descendants:
            if candidate.is_file():
                candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
        shutil.rmtree(result_root)
    result_root.mkdir(mode=0o700)


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _reserve_loopback_port() -> int:
    """Ask the kernel for a currently unused TCP port for a fixed Docker mapping."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    if not 1 <= port <= 65_535:
        raise AssertionError(f"kernel reserved invalid PostgreSQL port {port}")
    return port


@contextmanager
def postgres_test_server(*, fixed_host_port: bool = False) -> Iterator[PostgresTestServer]:
    """Start a disposable real PostgreSQL server or fail loudly.

    This fixture intentionally has no SQLite/in-memory fallback and never skips
    when Docker, the configured image, or PostgreSQL itself is unavailable.
    """

    image = os.environ.get("YAYA_TEST_POSTGRES_IMAGE", "postgres:15").strip()
    if not image:
        raise AssertionError("YAYA_TEST_POSTGRES_IMAGE cannot be blank")
    try:
        _docker("version")
        _docker("image", "inspect", image)
    except (FileNotFoundError, subprocess.SubprocessError) as error:
        raise AssertionError(
            f"real PostgreSQL test dependency is unavailable: {type(error).__name__}"
        ) from error

    suffix = uuid.uuid4().hex[:12]
    container_name = f"yaya-postgres-test-{suffix}"
    password = f"yaya-test-{uuid.uuid4().hex}"
    requested_port = _reserve_loopback_port() if fixed_host_port else None
    started = False
    startup_error: BaseException | None = None
    try:
        _docker(
            "run",
            "--detach",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "POSTGRES_USER=yaya_test",
            "--env",
            "POSTGRES_DB=yaya_test",
            "--publish",
            (
                f"127.0.0.1:{requested_port}:5432"
                if requested_port is not None
                else "127.0.0.1::5432"
            ),
            image,
        )
        started = True
        deadline = time.monotonic() + 45
        # The official image briefly starts an initialization server on its Unix
        # socket. Probe TCP so only the final published server can report ready.
        while time.monotonic() < deadline:
            ready = _docker(
                "exec",
                container_name,
                "pg_isready",
                "--host",
                "127.0.0.1",
                "--username",
                "yaya_test",
                "--dbname",
                "yaya_test",
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise AssertionError("real PostgreSQL did not become ready within 45 seconds")

        published = _docker("port", container_name, "5432/tcp").stdout.strip()
        match = re.search(r":([0-9]{1,5})$", published)
        if match is None:
            raise AssertionError(f"cannot resolve PostgreSQL test port from {published!r}")
        port = int(match.group(1))
        if not 1 <= port <= 65_535:
            raise AssertionError(f"Docker published invalid PostgreSQL port {port}")
        if requested_port is not None and port != requested_port:
            raise AssertionError(
                f"Docker ignored fixed PostgreSQL port {requested_port} and published {port}"
            )
        yield PostgresTestServer(
            dsn=(f"postgresql://yaya_test:{password}@127.0.0.1:{port}/yaya_test?connect_timeout=5"),
            container_name=container_name,
        )
    except BaseException as error:
        startup_error = error
        raise
    finally:
        if started:
            cleanup = _docker("rm", "--force", container_name, check=False)
            if cleanup.returncode != 0 and startup_error is None:
                raise AssertionError(
                    f"failed to remove PostgreSQL test container {container_name}: "
                    f"{cleanup.stderr.strip()}"
                )


__all__ = [
    "PostgresTestServer",
    "postgres_test_server",
    "reset_sandbox_recovery_results",
]
