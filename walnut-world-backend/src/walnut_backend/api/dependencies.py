"""Trusted contract context exposed to HTTP and WSS handlers."""

from __future__ import annotations

from starlette.requests import HTTPConnection
from yaya_agent_contracts import ActorRef, ContentRef, OperationContext


def get_operation_context(connection: HTTPConnection) -> OperationContext:
    """Return context derived by transport middleware, never by request bodies."""
    return connection.state.operation_context


__all__ = ["ActorRef", "ContentRef", "OperationContext", "get_operation_context"]
