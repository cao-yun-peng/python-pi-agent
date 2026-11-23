"""Offline coordination contract, timeouts and compare-release invariants."""

import asyncio
from dataclasses import replace
import unittest

from pi_agent_py.errors import (
    LeaseStorageError,
    LeaseTimeoutError,
    SessionValidationError,
)
from pi_agent_py.storage.lease import InMemoryLeaseManager
from pi_agent_py.storage.redis_lease import RedisLeaseManager, RedisLeaseSettings
from pi_agent_py.testing_clock import ManualLeaseClock


class SessionLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualLeaseClock()
        self.manager = InMemoryLeaseManager(clock=self.clock)

    async def test_exclusion_expiry_and_stale_release_with_same_owner_and_claim(self):
        first = await self.manager.acquire("s", "w", "c", 1)
        self.assertIsNone(await self.manager.acquire("s", "other", "other", 1))
        self.clock.advance(1)
        second = await self.manager.acquire("s", "w", "c", 1)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertFalse(await self.manager.release(first))
        self.assertIsNone(await self.manager.renew(first, 1))
        self.assertTrue(await self.manager.release(second))
        self.assertFalse(await self.manager.release(second))

    async def test_compare_renew_and_release_check_every_identity_field(self):
        lease = await self.manager.acquire("s", "w", "c", 1)
        for changed in (
            replace(lease, owner_id="wrong"),
            replace(lease, claim_id="wrong"),
            replace(lease, nonce="wrong"),
            replace(lease, session_id="other"),
        ):
            self.assertIsNone(await self.manager.renew(changed, 5))
            self.assertFalse(await self.manager.release(changed))
        self.clock.advance(0.5)
        renewed = await self.manager.renew(lease, 1)
        self.clock.advance(0.5)
        self.assertIsNotNone(await self.manager.renew(lease, 1))
        self.assertEqual(renewed.nonce, lease.nonce)

    async def test_clear_loses_lease_and_does_not_revive_it(self):
        lease = await self.manager.acquire("s", "w", "c", 1)
        self.manager.clear()
        self.assertIsNone(await self.manager.renew(lease, 1))
        self.assertFalse(await self.manager.release(lease))

    async def test_invalid_duration_and_ids_fail_at_boundary(self):
        for ttl in (0, -1, True, float("inf"), float("nan"), 0.0001):
            with self.subTest(ttl=ttl), self.assertRaises(SessionValidationError):
                await self.manager.acquire("s", "w", "c", ttl)
        with self.assertRaises(SessionValidationError):
            await self.manager.acquire(" ", "w", "c", 1)

    async def test_redis_timeout_and_cancellation_do_not_start_hidden_tasks(self):
        class BlockedClient:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def eval(self, *args):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()

        client = BlockedClient()
        manager = RedisLeaseManager(
            client, clock=self.clock, settings=RedisLeaseSettings(command_timeout=0.5)
        )
        task = asyncio.create_task(manager.acquire("s", "w", "c", 1))
        await client.started.wait()
        self.clock.advance(0.5)
        with self.assertRaises(LeaseTimeoutError):
            await task
        self.assertTrue(client.cancelled.is_set())
        self.assertEqual(self.clock.pending_timeouts, 0)
        client.cancelled.clear()
        task = asyncio.create_task(manager.acquire("s", "w", "c", 1))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.clock.pending_timeouts, 0)

    async def test_redis_driver_and_protocol_errors_are_not_occupancy(self):
        class Client:
            async def eval(self, *args):
                raise OSError("private diagnostics")

        with self.assertRaises(LeaseStorageError) as caught:
            await RedisLeaseManager(Client()).acquire("s", "w", "c", 1)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertNotIn("private", str(caught.exception))

        class Malformed:
            async def eval(self, *args):
                return True

        with self.assertRaises(LeaseStorageError):
            await RedisLeaseManager(Malformed()).acquire("s", "w", "c", 1)

    async def test_malformed_busy_reply_is_not_reported_as_contention(self):
        class Malformed:
            async def eval(self, *args):
                return [0, None, None]

        with self.assertRaises(LeaseStorageError):
            await RedisLeaseManager(Malformed()).acquire("s", "w", "c", 1)

    async def test_clock_can_advance_while_expired_timeout_unwinds_cleanup(self):
        expired, cleanup = asyncio.Event(), asyncio.Event()

        async def timed():
            async with self.clock.timeout(0.5):
                try:
                    await asyncio.Event().wait()
                finally:
                    expired.set()
                    await cleanup.wait()

        task = asyncio.create_task(timed())
        await asyncio.sleep(0)
        self.clock.advance(0.5)
        await expired.wait()
        self.clock.advance(0.1)
        cleanup.set()
        with self.assertRaises(TimeoutError):
            await task
        self.assertEqual(self.clock.pending_timeouts, 0)
