"""Explicit ownership and reverse-order cleanup for application assembly."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from .errors import RuntimeCloseError, RuntimeStateError

T = TypeVar("T")
ResourceOwnership = Literal["owned", "borrowed"]
Cleanup = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ManagedResource(Generic[T]):
    """One assembled value plus an explicit ownership decision."""

    name: str
    value: T
    ownership: ResourceOwnership = "borrowed"
    cleanup: Cleanup | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("managed resource name must not be empty")
        if self.ownership not in {"owned", "borrowed"}:
            raise ValueError("unknown managed resource ownership")
        if self.ownership == "borrowed" and self.cleanup is not None:
            raise ValueError("borrowed resources cannot register cleanup")


@dataclass(frozen=True, slots=True)
class _CleanupEntry:
    name: str
    callback: Cleanup


class LifecycleScope:
    """Owns cleanup callbacks without becoming a general service locator."""

    def __init__(self, *, name: str = "runtime") -> None:
        if not name.strip():
            raise ValueError("lifecycle scope name must not be empty")
        self._name = name
        self._entries: list[_CleanupEntry] = []
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def manage(self, resource: ManagedResource[T]) -> T:
        """Register an owned resource and return its value unchanged."""
        if self._closed:
            raise RuntimeStateError(f'lifecycle scope "{self._name}" is closed')
        if resource.ownership == "owned":
            cleanup = resource.cleanup or _infer_cleanup(resource.value)
            if cleanup is not None:
                self._entries.append(_CleanupEntry(resource.name, cleanup))
        return resource.value

    def push(self, name: str, cleanup: Cleanup) -> None:
        """Register one explicit owned cleanup callback."""
        self.manage(
            ManagedResource(
                name=name,
                value=cleanup,
                ownership="owned",
                cleanup=cleanup,
            )
        )

    async def aclose(self) -> None:
        """Attempt every cleanup in reverse order; safe to call repeatedly."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(reversed(self._entries))
            self._entries.clear()

        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        for entry in entries:
            try:
                result = entry.callback()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except BaseException as error:
                failures.append(error)

        if cancellation is not None:
            raise cancellation
        if failures:
            raise RuntimeCloseError(
                f'lifecycle scope "{self._name}" had '
                f"{len(failures)} cleanup failure(s)",
                failures=tuple(failures),
            ) from failures[0]


def _infer_cleanup(value: object) -> Cleanup | None:
    async_close = getattr(value, "aclose", None)
    if callable(async_close):
        return async_close
    close = getattr(value, "close", None)
    if callable(close):
        return close
    return None
