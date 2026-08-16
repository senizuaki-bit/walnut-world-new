"""Production command-line entrypoints for migration, HTTP and worker processes."""

from __future__ import annotations

import argparse
import asyncio
import signal
import threading
from http.server import ThreadingHTTPServer

from .composition import (
    create_learner_worker_composition,
    create_production_composition,
    verify_contract_manifest,
)
from .config import LearnerWorkerSettings, ProductionSettings
from .database import PostgresDatabase
from .http_api import AgentHttpApi, serve_http
from .http_router import ProductionHttpApi
from .product_http_api import ProductHttpApi


async def _migrate(settings: ProductionSettings) -> None:
    verify_contract_manifest(settings.contracts_root)
    await PostgresDatabase(settings.database_dsn).migrate()


async def _worker(settings: ProductionSettings) -> None:
    composition = await create_production_composition(settings)
    stop = asyncio.Event()
    workers = (
        asyncio.create_task(composition.worker.run_forever(stop)),
        asyncio.create_task(composition.student_chain_worker.run_forever(stop)),
    )
    try:
        await asyncio.gather(*workers)
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)


async def _learner_worker(settings: LearnerWorkerSettings) -> None:
    composition = await create_learner_worker_composition(settings)
    stop = asyncio.Event()
    worker = asyncio.create_task(composition.learner_worker.run_forever(stop))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Let an in-flight projection finish its fenced transaction before the
        # process exits.  Idle workers observe this immediately.
        stop.set()
        await worker
        raise


async def _serve(settings: ProductionSettings) -> None:
    composition = await create_production_composition(settings)
    game_api = AgentHttpApi(
        application=composition.application,
        authenticator=composition.authenticator,
        validator=composition.validator,
        student_chain=composition.student_chain_application,
    )
    product_api = ProductHttpApi(
        application=composition.product_application,
        draft_application=composition.draft_application,
        authenticator=composition.authenticator,
        validator=composition.validator,
    )
    api = ProductionHttpApi(game=game_api, product=product_api)
    created = threading.Event()
    server_box: list[ThreadingHTTPServer] = []

    def capture_server(server: ThreadingHTTPServer) -> None:
        server_box.append(server)
        created.set()

    server_thread = threading.Thread(
        target=serve_http,
        args=(api, settings.http_host, settings.http_port),
        kwargs={"server_created": capture_server},
        name="yaya-agent-http",
        daemon=False,
    )
    server_thread.start()
    if not await asyncio.to_thread(created.wait, 30) or not server_box:
        raise RuntimeError("production HTTP server did not bind within 30 seconds")
    server = server_box[0]
    try:
        while server_thread.is_alive():
            await asyncio.sleep(0.5)
        raise RuntimeError("production HTTP server stopped unexpectedly")
    finally:
        await asyncio.to_thread(server.shutdown)
        await asyncio.to_thread(server_thread.join, 10)
        if server_thread.is_alive():
            raise RuntimeError("production HTTP server did not stop within 10 seconds")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m yaya_agent_backend")
    parser.add_argument(
        "command",
        choices=("learner-worker", "migrate", "serve", "worker"),
        help="production process role",
    )
    args = parser.parse_args()
    if hasattr(signal, "SIGBREAK"):
        # Windows service supervisors commonly deliver CTRL_BREAK_EVENT to a
        # process group.  Python's default console handler otherwise exits with
        # NTSTATUS 0xC000013A before the async finally blocks can close HTTP.
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        if args.command == "learner-worker":
            asyncio.run(_learner_worker(LearnerWorkerSettings.from_env()))
        else:
            settings = ProductionSettings.from_env()
            action = {
                "migrate": _migrate,
                "serve": _serve,
                "worker": _worker,
            }[args.command]
            asyncio.run(action(settings))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
