"""Narrow, scripted grant/lease dependencies; no duplicate runtime loop."""

import asyncio

from pi_agent_py.execution_domain import ExecutionAuthority
from pi_agent_py.storage.execution_transitions import renew_execution, release_execution
from pi_agent_py.storage.lease import InMemoryLeaseManager


class ControlledLeases(InMemoryLeaseManager):
    def __init__(self, clock, trace):
        super().__init__(clock=clock)
        self.trace = trace
        self.block = False
        self.failure = None
        self.renew_started = asyncio.Event()
        self.renew_cancelled = asyncio.Event()
        self.renews = 0

    async def renew(self, lease, ttl):
        self.trace.append("redis-renew")
        self.renews += 1
        self.renew_started.set()
        if self.failure:
            raise self.failure
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.renew_cancelled.set()
        return await super().renew(lease, ttl)

    async def release(self, lease):
        self.trace.append("redis-release")
        return await super().release(lease)


class ControlledGrants:
    def __init__(self, clock, grant, trace):
        self.clock = clock
        self.authority = ExecutionAuthority(
            grant.session_id, grant.epoch, grant, grant.run_id
        )
        self.trace = trace
        self.renews = 0
        self.releases = 0
        self.failure = None
        self.release_failure = None
        self.block = False
        self.renew_started = asyncio.Event()
        self.release_started = asyncio.Event()
        self.release_gate = None

    async def renew_grant(self, grant, ttl):
        self.trace.append("pg-renew")
        self.renews += 1
        self.renew_started.set()
        if self.failure:
            raise self.failure
        if self.block:
            await asyncio.Event().wait()
        self.authority = renew_execution(
            self.authority, grant, now=self.clock.unix_time(), ttl=ttl
        )
        return self.authority.grant

    async def release_grant(self, grant):
        self.trace.append("pg-release")
        self.releases += 1
        self.release_started.set()
        if self.release_gate:
            await self.release_gate.wait()
        if self.release_failure:
            raise self.release_failure
        if self.authority.grant is None:
            return False
        self.authority = release_execution(self.authority, grant)
        return True


async def pump_until(predicate):
    """Bounded scheduler yields; not a wall-clock or TTL wait."""
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("expected observable state did not arrive")
