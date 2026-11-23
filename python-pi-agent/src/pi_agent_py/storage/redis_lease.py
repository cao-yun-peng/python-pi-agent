"""Redis 7 single-key Lua coordination with nonce compare-renew/release."""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from secrets import token_hex
from typing import Protocol

from ..errors import LeaseStorageError, LeaseTimeoutError, SessionValidationError
from ..execution_domain import SessionLease, _text, lease_ttl_ms
from ..lease_clock import SystemLeaseClock
from ..ports import LeaseClock


ACQUIRE_LEASE_LUA = """
local acquired = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2])
if not acquired then return {0, 0, 0} end
local now = redis.call('TIME')
return {1, now[1], now[2]}
"""
RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return {0, 0, 0} end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
local now = redis.call('TIME')
return {1, now[1], now[2]}
"""
RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisLeaseClient(Protocol):
    """Borrowed redis.asyncio-compatible client; this adapter never closes it."""

    async def eval(
        self, script: str, numkeys: int, *keys_and_args: str | int
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RedisLeaseSettings:
    command_timeout: float = 2.0
    key_prefix: str = "pi:lease:session:"

    def __post_init__(self) -> None:
        if (
            isinstance(self.command_timeout, bool)
            or not isinstance(self.command_timeout, (int, float))
            or not isfinite(self.command_timeout)
            or self.command_timeout <= 0
        ):
            raise SessionValidationError(
                "Redis command_timeout must be finite and positive"
            )
        _text(self.key_prefix, "key_prefix")
        if "{" in self.key_prefix or "}" in self.key_prefix:
            raise SessionValidationError("Redis prefix must not contain hash tags")


class RedisLeaseManager:
    def __init__(
        self,
        client: RedisLeaseClient,
        *,
        settings: RedisLeaseSettings = RedisLeaseSettings(),
        clock: LeaseClock | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._client, self._settings = client, settings
        self._clock = clock if clock is not None else SystemLeaseClock()
        self._nonce = (
            nonce_factory if nonce_factory is not None else lambda: token_hex(16)
        )

    def key_for_session(self, session_id: str) -> str:
        _text(session_id, "session_id")
        encoded = sha256(session_id.encode("utf-8")).hexdigest()
        return self._settings.key_prefix + "{" + encoded + "}"

    @staticmethod
    def _value(lease: SessionLease) -> str:
        # Expiry is deliberately excluded so renewal never changes ownership.
        return json.dumps(
            {
                "v": 1,
                "session": lease.session_id,
                "owner": lease.owner_id,
                "claim": lease.claim_id,
                "nonce": lease.nonce,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    async def _eval(
        self, script: str, key: str, value: str, ttl_ms: int | None = None
    ) -> object:
        try:
            async with self._clock.timeout(self._settings.command_timeout):
                if ttl_ms is None:
                    return await self._client.eval(script, 1, key, value)
                return await self._client.eval(script, 1, key, value, ttl_ms)
        except TimeoutError as error:
            raise LeaseTimeoutError(
                "Redis lease operation timed out; outcome may be unknown"
            ) from error
        except Exception as error:
            raise LeaseStorageError(
                "Redis lease operation failed; outcome may be unknown"
            ) from error

    @staticmethod
    def _deadline(result: object, ttl_ms: int) -> float | None:
        try:
            if (
                not isinstance(result, (list, tuple))
                or len(result) != 3
                or type(result[0]) is not int
                or result[0] not in (0, 1)
            ):
                raise ValueError("invalid Lua lease reply")
            if result[0] == 0:
                if any(type(value) is not int or value != 0 for value in result[1:]):
                    raise ValueError("invalid Lua contention reply")
                return None
            values = []
            for value in result[1:]:
                if type(value) is int:
                    values.append(value)
                elif isinstance(value, (bytes, str)) and value.isdigit():
                    values.append(int(value))
                else:
                    raise ValueError("invalid Redis server time")
            seconds, micros = values
            if seconds < 0 or not 0 <= micros < 1_000_000:
                raise ValueError("invalid Redis server time range")
            deadline = seconds + micros / 1_000_000 + ttl_ms / 1000
            if not isfinite(deadline):
                raise ValueError("invalid Redis deadline")
            return deadline
        except (ValueError, TypeError, OverflowError) as error:
            raise LeaseStorageError("invalid Redis lease protocol reply") from error

    async def acquire(
        self, session_id: str, owner_id: str, claim_id: str, ttl: float
    ) -> SessionLease | None:
        milliseconds = lease_ttl_ms(ttl)
        # This temporary envelope validates identity before performing any I/O.
        proposed = SessionLease(session_id, owner_id, claim_id, self._nonce(), 0)
        result = await self._eval(
            ACQUIRE_LEASE_LUA,
            self.key_for_session(session_id),
            self._value(proposed),
            milliseconds,
        )
        deadline = self._deadline(result, milliseconds)
        return (
            None
            if deadline is None
            else SessionLease(session_id, owner_id, claim_id, proposed.nonce, deadline)
        )

    async def renew(self, lease: SessionLease, ttl: float) -> SessionLease | None:
        milliseconds = lease_ttl_ms(ttl)
        result = await self._eval(
            RENEW_LEASE_LUA,
            self.key_for_session(lease.session_id),
            self._value(lease),
            milliseconds,
        )
        deadline = self._deadline(result, milliseconds)
        return (
            None
            if deadline is None
            else SessionLease(
                lease.session_id, lease.owner_id, lease.claim_id, lease.nonce, deadline
            )
        )

    async def release(self, lease: SessionLease) -> bool:
        result = await self._eval(
            RELEASE_LEASE_LUA,
            self.key_for_session(lease.session_id),
            self._value(lease),
        )
        if type(result) is not int or result not in (0, 1):
            raise LeaseStorageError("invalid Redis release protocol reply")
        return result == 1
