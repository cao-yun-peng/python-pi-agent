"""Explicit, reversible suppression of SDK raw diagnostics in owned task scopes."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

# Names verified against the pinned SDK; filters affect only this owner's context.
_NAMES = (
    "client",
    "mcp.client.client",
    "mcp.client.stdio",
    "mcp.client.streamable_http",
    "mcp.shared.jsonrpc_dispatcher",
    "mcp.shared.dispatcher",
    "mcp.shared.tool_name_validation",
    "httpx2",
    "mcp.os.win32.utilities",
    "mcp.os.posix.utilities",
    "asyncio",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
)
_SCOPE: ContextVar[object | None] = ContextVar("mcp_private_diagnostics", default=None)


@contextmanager
def sdk_log_privacy() -> Iterator[None]:
    marker = object()

    class Filter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return _SCOPE.get() is not marker

    guard = Filter()
    loggers = [logging.getLogger(name) for name in _NAMES]
    token = _SCOPE.set(marker)
    try:
        for logger in loggers:
            logger.addFilter(guard)
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(guard)
        _SCOPE.reset(token)
