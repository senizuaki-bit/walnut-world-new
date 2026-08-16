"""ASGI entrypoint for the Walnut World production backend."""

from walnut_backend.api.app import create_app

app = create_app()
