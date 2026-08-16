"""Executable entry point for the private relay process."""

from __future__ import annotations

import uvicorn

from .app import create_relay_app
from .config import RelaySettings


def main() -> None:
    settings = RelaySettings.from_env()
    uvicorn.run(
        create_relay_app(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()


__all__ = ["main"]
