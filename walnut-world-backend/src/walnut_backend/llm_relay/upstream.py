"""One-shot bounded DeepSeek/OpenAI-compatible upstream transport."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


class UpstreamAcknowledgementUnknown(ConnectionError):
    """The relay cannot prove whether the Provider accepted the only POST."""


class UpstreamResponseInvalid(RuntimeError):
    """The only Provider response cannot be represented by the relay protocol."""


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status: int
    content_type: str
    body: bytes
    body_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not 100 <= self.status <= 599:
            raise ValueError("Provider HTTP status is invalid")
        if not isinstance(self.content_type, str) or len(self.content_type) > 256:
            raise ValueError("Provider Content-Type is invalid")
        if not isinstance(self.body, bytes):
            raise TypeError("Provider response body must be bytes")
        object.__setattr__(self, "body_sha256", hashlib.sha256(self.body).hexdigest())


class UpstreamTransport(Protocol):
    async def post_completion(self, completion: Mapping[str, object]) -> ProviderHttpResponse: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class UrllibUpstreamTransport:
    """Perform exactly one POST; transport ambiguity is never retried."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        timeout_ms: int,
        max_response_bytes: int,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout_ms = timeout_ms
        self._maximum = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(endpoint={self._endpoint!r}, "
            f"timeout_ms={self._timeout_ms!r}, max_response_bytes={self._maximum!r})"
        )

    async def post_completion(self, completion: Mapping[str, object]) -> ProviderHttpResponse:
        return await asyncio.to_thread(self._post, completion)

    def _post(self, completion: Mapping[str, object]) -> ProviderHttpResponse:
        body = json.dumps(
            completion,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json; charset=utf-8",
                "accept": "application/json",
                "user-agent": "walnut-private-recoverable-relay/1",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout_ms / 1000) as response:
                return self._read_response(
                    response.status,
                    response.headers.get("Content-Type", ""),
                    response,
                )
        except urllib.error.HTTPError as error:
            return self._read_response(
                error.code,
                error.headers.get("Content-Type", ""),
                error,
            )
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
            raise UpstreamAcknowledgementUnknown(
                "Provider POST acknowledgement is unknown; dispatch is not retryable"
            ) from error

    def _read_response(
        self,
        status: int,
        content_type: str,
        response: object,
    ) -> ProviderHttpResponse:
        try:
            body = response.read(self._maximum + 1)  # type: ignore[attr-defined]
        except (http.client.HTTPException, TimeoutError, OSError) as error:
            raise UpstreamAcknowledgementUnknown(
                "Provider response acknowledgement is unknown; dispatch is not retryable"
            ) from error
        if len(body) > self._maximum:
            raise UpstreamResponseInvalid("Provider response exceeds max_response_bytes")
        return ProviderHttpResponse(status=status, content_type=content_type, body=body)


__all__ = [
    "ProviderHttpResponse",
    "UpstreamAcknowledgementUnknown",
    "UpstreamResponseInvalid",
    "UpstreamTransport",
    "UrllibUpstreamTransport",
]
