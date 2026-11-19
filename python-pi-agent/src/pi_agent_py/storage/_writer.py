"""SessionStore error translation and per-run durable commits."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from ..domain import (
    ControlDelivery,
    DurableEventIdentity,
    Message,
    SessionCompaction,
    SessionEventContext,
    SessionSnapshot,
    session_event_id,
)
from ..errors import AgentError, SessionStorageError
from ..ports import SessionStore


_StoreParameters = ParamSpec("_StoreParameters")
_StoreResult = TypeVar("_StoreResult")


async def _call_session_store(
    operation: Callable[_StoreParameters, Awaitable[_StoreResult]],
    /,
    *args: _StoreParameters.args,
    **kwargs: _StoreParameters.kwargs,
) -> _StoreResult:
    """Translate foreign errors at the persistence boundary."""
    try:
        return await operation(*args, **kwargs)
    except AgentError:
        raise
    except Exception as error:
        raise SessionStorageError("session store operation failed") from error


class _SessionWriter:
    """Append one message at a time while carrying the latest version."""

    def __init__(
        self,
        session_store: SessionStore,
        snapshot: SessionSnapshot,
        lock: asyncio.Lock,
    ) -> None:
        self._session_store = session_store
        self._snapshot = snapshot
        self._lock = lock

    @property
    def snapshot(self) -> SessionSnapshot:
        return self._snapshot

    async def commit(
        self,
        message: Message,
        event_context: SessionEventContext,
    ) -> DurableEventIdentity:
        async with self._lock:
            await self._refresh()
            self._snapshot = await _call_session_store(
                self._session_store.append,
                self._snapshot.session_id,
                self._snapshot.version,
                (message,),
                event_context=event_context,
            )
            return _latest_durable_identity(self._snapshot)

    async def deliver_control(
        self,
        message: Message,
        event_context: SessionEventContext,
        control_id: str,
        claim_id: str,
    ) -> DurableEventIdentity:
        async with self._lock:
            await self._refresh()
            self._snapshot = await _call_session_store(
                self._session_store.record_control,
                self._snapshot.session_id,
                self._snapshot.version,
                "control_delivered",
                ControlDelivery(control_id, claim_id, message),
                event_context=event_context,
            )
            return _latest_durable_identity(self._snapshot)

    async def compact(
        self,
        compaction: SessionCompaction,
        event_context: SessionEventContext,
    ) -> SessionSnapshot:
        async with self._lock:
            await self._refresh()
            self._snapshot = await _call_session_store(
                self._session_store.compact,
                self._snapshot.session_id,
                self._snapshot.version,
                compaction,
                event_context=event_context,
            )
        return self._snapshot

    async def _refresh(self) -> None:
        self._snapshot = await _call_session_store(
            self._session_store.load, self._snapshot.session_id
        )


def _latest_durable_identity(snapshot: SessionSnapshot) -> DurableEventIdentity:
    if snapshot.version <= 0:
        raise RuntimeError("durable commit did not advance the session version")
    return DurableEventIdentity(
        session_id=snapshot.session_id,
        sequence=snapshot.version,
        event_id=session_event_id(snapshot.session_id, snapshot.version),
    )
