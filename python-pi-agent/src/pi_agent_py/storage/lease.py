"""Single-event-loop coordination substitute with an injected clock."""

from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_hex

from ..execution_domain import SessionLease, _text, lease_ttl_ms, same_lease
from ..lease_clock import SystemLeaseClock
from ..ports import LeaseClock


@dataclass(frozen=True, slots=True)
class _Entry:
    lease: SessionLease
    deadline: float


class InMemoryLeaseManager:
    """Instance-local state, no timers or I/O; not a distributed lock.

    nonce_factory is a test seam and must return a fresh value per acquisition.
    Client deadlines are monotonic; expires_at is only the Unix-time envelope.
    """

    def __init__(
        self,
        *,
        clock: LeaseClock | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock if clock is not None else SystemLeaseClock()
        self._nonce = (
            nonce_factory if nonce_factory is not None else lambda: token_hex(16)
        )
        self._entries: dict[str, _Entry] = {}

    def _current(self, session_id: str) -> _Entry | None:
        current = self._entries.get(session_id)
        if current is not None and current.deadline <= self._clock.monotonic():
            del self._entries[session_id]
            return None
        return current

    async def acquire(
        self, session_id: str, owner_id: str, claim_id: str, ttl: float
    ) -> SessionLease | None:
        milliseconds = lease_ttl_ms(ttl)
        for name, value in (
            ("session_id", session_id),
            ("owner_id", owner_id),
            ("claim_id", claim_id),
        ):
            _text(value, name)
        if self._current(session_id) is not None:
            return None
        lease = SessionLease(
            session_id,
            owner_id,
            claim_id,
            self._nonce(),
            self._clock.unix_time() + milliseconds / 1000,
        )
        self._entries[session_id] = _Entry(
            lease, self._clock.monotonic() + milliseconds / 1000
        )
        return lease

    async def renew(self, lease: SessionLease, ttl: float) -> SessionLease | None:
        milliseconds = lease_ttl_ms(ttl)
        current = self._current(lease.session_id)
        if current is None or not same_lease(current.lease, lease):
            return None
        renewed = SessionLease(
            lease.session_id,
            lease.owner_id,
            lease.claim_id,
            lease.nonce,
            self._clock.unix_time() + milliseconds / 1000,
        )
        self._entries[lease.session_id] = _Entry(
            renewed, self._clock.monotonic() + milliseconds / 1000
        )
        return renewed

    async def release(self, lease: SessionLease) -> bool:
        current = self._current(lease.session_id)
        if current is None or not same_lease(current.lease, lease):
            return False
        del self._entries[lease.session_id]
        return True

    def clear(self) -> None:
        """Testing/admin seam: invalidate only this fake manager's coordination."""
        self._entries.clear()
