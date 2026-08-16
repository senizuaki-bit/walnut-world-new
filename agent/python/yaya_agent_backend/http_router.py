"""Production HTTP routing without coupling Product semantics into Game."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from .http_api import HttpApi, HttpResponse


class ProductionHttpApi:
    def __init__(self, *, game: HttpApi, product: HttpApi) -> None:
        self._game = game
        self._product = product

    async def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> HttpResponse:
        raw_path = urlsplit(target).path
        destination = (
            self._product
            if raw_path == "/product-experience" or raw_path.startswith("/product-experience/")
            else self._game
        )
        return await destination.handle(method, target, headers, body)


__all__ = ["ProductionHttpApi"]
