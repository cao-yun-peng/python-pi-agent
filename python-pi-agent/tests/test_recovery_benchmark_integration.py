"""Explicit benchmark smoke with two independent PG pools and Redis clients."""

import unittest
from uuid import uuid4

from pi_agent_py.application.benchmark_report import BenchmarkSettings
from pi_agent_py.application.recovery_benchmark import (
    BenchmarkExecutionStore,
    BenchmarkPeer,
    BenchmarkTrace,
    run_benchmark,
)
from pi_agent_py.storage.postgres import PostgresSessionStore
from pi_agent_py.storage.redis_lease import RedisLeaseManager, RedisLeaseSettings
from postgres_fixture import PostgresFixture, _DSN
from test_redis_lease import REDIS_URL, redis_client


@unittest.skipUnless(_DSN and REDIS_URL, "set explicit PG/Redis test endpoints")
class RecoveryBenchmarkIntegrationTests(
    PostgresFixture, unittest.IsolatedAsyncioTestCase
):
    async def test_four_fault_scenarios_have_measured_and_durable_evidence(self):
        trace = BenchmarkTrace()
        clients, peers = [], []
        try:
            for _ in range(2):
                pool = await self.new_pool()
                client = await redis_client()
                clients.append(client)
                store = BenchmarkExecutionStore(pool, trace)
                peers.append(
                    BenchmarkPeer(
                        PostgresSessionStore(pool),
                        store,
                        RedisLeaseManager(
                            client,
                            settings=RedisLeaseSettings(
                                key_prefix="pi:bench:test:" + self.schema + ":"
                            ),
                        ),
                    )
                )
            result = await run_benchmark(
                tuple(peers),
                trace,
                BenchmarkSettings(samples=2, warmup=1),
                {"code_commit": uuid4().hex, "condition": "isolated real services"},
            )
            self.assertEqual(result["status"], "valid")
            self.assertEqual(
                [batch["correct"] for batch in result["batches"]], [2, 2, 2, 2]
            )
            self.assertTrue(
                all(batch["elapsed_seconds"] > 0 for batch in result["batches"])
            )
        finally:
            for client in clients:
                await client.aclose()
