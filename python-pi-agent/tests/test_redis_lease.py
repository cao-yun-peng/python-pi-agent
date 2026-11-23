"""Explicit local Redis integration. Only unique test-prefixed keys are deleted."""

import asyncio
from dataclasses import replace
import importlib
import os
import unittest
from uuid import uuid4

from pi_agent_py.errors import LeaseStorageError
from pi_agent_py.storage.redis_lease import RedisLeaseManager, RedisLeaseSettings

REDIS_URL = os.environ.get("PI_AGENT_REDIS_TEST_URL")


async def redis_client():
    driver = importlib.import_module("redis.asyncio")
    retry = importlib.import_module("redis.asyncio.retry").Retry
    backoff = importlib.import_module("redis.backoff").NoBackoff
    return driver.from_url(
        REDIS_URL, socket_timeout=1, socket_connect_timeout=1, retry=retry(backoff(), 0)
    )


@unittest.skipUnless(REDIS_URL, "set PI_AGENT_REDIS_TEST_URL for Redis integration")
class RedisLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = [await redis_client(), await redis_client()]
        self.prefix = "pi:test:" + uuid4().hex + ":"
        self.managers = [
            RedisLeaseManager(c, settings=RedisLeaseSettings(key_prefix=self.prefix))
            for c in self.clients
        ]
        self.manager = self.managers[0]

    async def asyncTearDown(self):
        async for key in self.clients[0].scan_iter(match=self.prefix + "*"):
            await self.clients[0].delete(key)
        for client in self.clients:
            await client.aclose()

    async def test_ten_concurrent_clients_have_one_holder(self):
        results = await asyncio.gather(
            *(
                self.managers[i % 2].acquire("s", f"w{i}", f"c{i}", 10)
                for i in range(10)
            )
        )
        leases = [lease for lease in results if lease is not None]
        self.assertEqual(len(leases), 1)
        self.assertTrue(await self.clients[0].ping())
        self.assertTrue(await self.manager.release(leases[0]))

    async def test_expiry_new_nonce_prevents_old_holder_deleting_new_lease(self):
        first = await self.manager.acquire("s", "w", "c", 10)
        self.assertGreater(
            await self.clients[0].pttl(self.manager.key_for_session("s")), 0
        )
        # Server-side expiry, without a wall-clock sleep.
        await self.clients[0].pexpireat(self.manager.key_for_session("s"), 1)
        self.assertIsNone(await self.manager.renew(first, 10))
        second = await self.managers[1].acquire("s", "w", "c", 10)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertFalse(await self.manager.release(first))
        self.assertIsNotNone(await self.manager.renew(second, 10))

    async def test_identity_and_stale_expiry_metadata_compare_correctly(self):
        first = await self.manager.acquire("s", "w", "c", 10)
        for changed in (
            replace(first, owner_id="other"),
            replace(first, claim_id="other"),
            replace(first, nonce="other"),
        ):
            self.assertIsNone(await self.manager.renew(changed, 30))
            self.assertFalse(await self.manager.release(changed))
        renewed = await self.manager.renew(first, 20)
        self.assertGreater(renewed.expires_at, first.expires_at)
        self.assertTrue(await self.manager.release(first))

    async def test_arbitrary_ids_cannot_inject_hash_tags_or_ambiguous_values(self):
        sid = "s:{evil}:中文"
        lease = await self.manager.acquire(sid, "owner:one", "claim:two", 10)
        key = self.manager.key_for_session(sid)
        self.assertEqual(key.count("{"), 1)
        self.assertEqual(key.count("}"), 1)
        self.assertNotIn("evil", key)
        self.assertIsNotNone(await self.manager.renew(lease, 10))
        self.assertTrue(await self.manager.release(lease))

    async def test_foreign_value_is_not_deleted_and_wrong_type_is_storage_error(self):
        lease = await self.manager.acquire("s", "w", "c", 10)
        key = self.manager.key_for_session("s")
        await self.clients[0].set(key, "foreign")
        self.assertFalse(await self.manager.release(lease))
        self.assertEqual(await self.clients[0].get(key), b"foreign")
        await self.clients[0].delete(key)
        await self.clients[0].lpush(key, "wrong-type")
        with self.assertRaises(LeaseStorageError):
            await self.manager.renew(lease, 10)
