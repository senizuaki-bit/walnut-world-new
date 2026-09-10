"""Godot -> actual main FastAPI route/service with deterministic external ports.

No real model, audio provider, database or compiler is claimed by this test.
The separate bug_practice_live_test.gd exercises those real dependencies.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "walnut-world-backend"
sys.path[:0] = [str(BACKEND), str(BACKEND / "src"), str(ROOT / "agent/python")]
from tests.unit.test_bug_practice import setup
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
import uvicorn

app = create_app(Settings.for_test(
    contract_path=DEFAULT_CONTRACT_PATH,
    contract_release_path=BACKEND / "contract-release.json",
))
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        service, _ = setup()
        service.reads.passed = True
        original_grade = service.judge.grade

        async def grade(problem, source):
            result = await original_grade(problem, source)
            result["status"] = "SUCCEEDED" if result["correct"] else "REJECTED"
            return result

        service.judge.grade = grade
        application.state.bug_practice = service
        yield
        await service.close()


app.router.lifespan_context = lifespan


def main():
    engine = os.environ.get("GODOT_EXE") or shutil.which("Godot_v4.7.1-stable_win64_console.exe")
    if not engine:
        raise SystemExit("Set GODOT_EXE to Godot 4.7.1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("Test server did not start")
            env = dict(os.environ, WALNUT_PRACTICE_HTTP_URL=f"http://127.0.0.1:{port}")
            command = [engine, "--path", str(ROOT / "walnut-world-frontend"), "--script", "res://scripts/testing/bug_practice_http_test.gd"]
            if not env.get("WALNUT_PRACTICE_CAPTURE"):
                command.append("--headless")
            else:
                command += ["--rendering-method", "gl_compatibility", "--resolution", "1280x720"]
            startup = None
            if os.name == "nt":
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
            result = subprocess.run(command, env=env, timeout=90, startupinfo=startup)
            return result.returncode
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
