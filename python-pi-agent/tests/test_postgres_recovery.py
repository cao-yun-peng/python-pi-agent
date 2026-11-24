"""Two independent pool/client Workers and a real process crash; FakeProvider only."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    DistributedExecution,
    DistributedExecutionSettings,
    PostgresSessionStore,
    PostgresExecutionStore,
    RedisLeaseManager,
    RedisLeaseSettings,
    RegisteredRecoveryDispatcher,
    SessionRecoveryWorker,
    RecoveryWorkerSettings,
    RunSubmission,
    Finish,
    TextDelta,
    ToolCall,
    ToolDefinition,
    FunctionTool,
    ToolOutput,
    ClaimExecutionRequest,
    ExecutionStepRequest,
    DispatchModel,
    CommitToolResults,
    ExecutionGrant,
    execution_operation,
)
from pi_agent_py.errors import SessionConflictError
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled
from pi_agent_py.testing_clock import ManualLeaseClock
from postgres_fixture import PostgresFixture, _DSN
from test_redis_lease import REDIS_URL, redis_client
from test_postgres_execution import LostReplyPool
from lease_fakes import pump_until

_RESTART_CONTAINER = os.environ.get("PI_AGENT_REDIS_TEST_RESTART_CONTAINER")


@unittest.skipUnless(_DSN and REDIS_URL, "set explicit PG/Redis test endpoints")
class PostgresRecoveryTests(PostgresFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clients = [await redis_client(), await redis_client()]
        self.prefix = "pi:recovery:test:" + uuid4().hex + ":"
        self.pool_b = await self.new_pool()
        self.readers = [self.store, PostgresSessionStore(self.pool_b)]
        self.stores = [
            PostgresExecutionStore(self.pool),
            PostgresExecutionStore(self.pool_b),
        ]
        self.leases = [
            RedisLeaseManager(
                client, settings=RedisLeaseSettings(key_prefix=self.prefix)
            )
            for client in self.clients
        ]
        self.clock = ManualLeaseClock()
        self.workers, self.runtimes, self.processes = [], [], []

    async def asyncTearDown(self):
        try:
            for process in self.processes:
                if process.returncode is None:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 10)
            for worker in self.workers:
                await worker.aclose()
            for runtime in self.runtimes:
                await runtime.aclose()
            async for key in self.clients[0].scan_iter(match=self.prefix + "*"):
                await self.clients[0].delete(key)
            for client in self.clients:
                await client.aclose()
        finally:
            await super().asyncTearDown()
        self.assertEqual(self.clock.pending_sleeps, 0)
        self.assertEqual(self.clock.pending_timeouts, 0)

    async def accept(self, sid="s", rid="r", **kwargs):
        return await self.stores[0].submit_run(
            RunSubmission(sid, rid, "hello", rid, "profile:v1", "hash", **kwargs)
        )

    def worker(self, node, provider=None, *, store=None, tools=(), settings=None):
        actual = store or self.stores[node]
        config = DistributedExecution(
            actual,
            self.leases[node],
            DistributedExecutionSettings("worker-" + str(node), "profile:v1", "hash"),
            self.clock,
        )

        async def factory(receipt):
            runtime = await (
                AgentRuntimeBuilder()
                .with_provider(
                    provider or FakeProvider([[TextDelta("done"), Finish()]])
                )
                .with_session_store(self.readers[node])
                .with_tools(tools)
                .with_graceful_shutdown(0)
                .with_distributed_execution(
                    actual,
                    self.leases[node],
                    settings=config.settings,
                    clock=self.clock,
                )
                .build()
            )
            self.runtimes.append(runtime)
            return runtime

        dispatcher = RegisteredRecoveryDispatcher(
            self.readers[node], config, {("profile:v1", "hash"): factory}
        )
        worker = SessionRecoveryWorker(
            config, dispatcher, settings=settings or RecoveryWorkerSettings()
        )
        self.workers.append(worker)
        return worker

    async def test_two_pools_and_clients_contend_with_one_physical_execution(self):
        await self.accept()
        both_scanned, continue_scan, model_entered, finish = (
            asyncio.Event() for _ in range(4)
        )
        scanned = 0

        class Store:
            def __init__(self, store):
                self.store = store

            def __getattr__(self, name):
                return getattr(self.store, name)

            async def list_candidates(self, limit, *, exclude=()):
                nonlocal scanned
                result = await self.store.list_candidates(limit, exclude=exclude)
                scanned += 1
                if scanned == 2:
                    both_scanned.set()
                await continue_scan.wait()
                return result

        calls = []

        class Provider(FakeProvider):
            async def stream(self, messages, tools=(), *, context):
                calls.append(context.run_id)
                model_entered.set()
                await finish.wait()
                async for event in super().stream(messages, tools, context=context):
                    yield event

        workers = [
            self.worker(
                node,
                Provider([[TextDelta("done"), Finish()]]),
                store=Store(self.stores[node]),
            )
            for node in (0, 1)
        ]
        tasks = [asyncio.create_task(w.run_once()) for w in workers]
        try:
            await asyncio.wait_for(both_scanned.wait(), 10)
            continue_scan.set()
            await asyncio.wait_for(model_entered.wait(), 10)
            self.assertEqual(calls, ["r"])
            finish.set()
            reports = await asyncio.wait_for(asyncio.gather(*tasks), 15)
            self.assertEqual(
                sorted(r.attempts[0].status for r in reports), ["busy", "completed"]
            )
            self.assertEqual((await self.stores[0].load_execution("s", "r")).attempt, 1)
        finally:
            finish.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_redis_clear_cannot_bypass_pg_then_other_worker_takes_over(self):
        await self.redis_loss_scenario(self.clients[0].delete)

    @unittest.skipUnless(
        _RESTART_CONTAINER, "opt in to restarting a dedicated test Redis container"
    )
    async def test_real_redis_restart_preserves_pg_epoch_and_stops_old_worker(self):
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        async def docker(*arguments):
            process = await asyncio.create_subprocess_exec(
                "docker",
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 20)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            self.assertEqual(
                process.returncode, 0, "dedicated Redis Docker action failed"
            )
            return output

        details = json.loads(await docker("inspect", _RESTART_CONTAINER))[0]
        # An endpoint alone never authorizes restarting a shared service.
        task = details["Config"]["Labels"].get("pi-agent.task", "")
        self.assertTrue(task.startswith("step7-"))
        self.assertEqual(details["Name"], "/pi-agent-redis-" + task)
        self.assertEqual(
            details["NetworkSettings"]["Ports"]["6379/tcp"],
            [{"HostIp": "127.0.0.1", "HostPort": str(urlsplit(REDIS_URL).port)}],
        )
        self.assertEqual(
            details["HostConfig"]["PortBindings"]["6379/tcp"],
            [{"HostIp": "127.0.0.1", "HostPort": str(urlsplit(REDIS_URL).port)}],
            "restart requires an explicitly fixed host port",
        )
        self.assertEqual(urlsplit(REDIS_URL).hostname, "127.0.0.1")

        async def restart(key):
            # Intentionally lose volatile coordination in this opt-in test service.
            await self.clients[0].config_set("save", "")
            await self.clients[0].config_set("appendonly", "no")
            await docker("restart", details["Id"])
            for client in self.clients:
                await client.connection_pool.disconnect()
            async with asyncio.timeout(10):
                while True:
                    try:
                        if await self.clients[0].ping():
                            break
                    except (RedisConnectionError, RedisTimeoutError):
                        pass
                    await asyncio.sleep(0.05)
            self.assertEqual(await self.clients[0].exists(key), 0)

        await self.redis_loss_scenario(restart)

    async def redis_loss_scenario(self, lose_key):
        await self.accept()
        first_provider = FakeProvider([[WaitUntilCancelled()]])
        first = self.worker(0, first_provider)
        task = asyncio.create_task(first.run_once())
        await asyncio.wait_for(first_provider.blocked.wait(), 10)
        await pump_until(lambda: self.clock.pending_sleeps == 1)
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT owner_id,claim_id,epoch,extract(epoch FROM lease_expires_at)::double precision AS expires_at FROM sessions WHERE session_id='s'"
            )
        old = ExecutionGrant(
            "s",
            row["owner_id"],
            row["claim_id"],
            row["epoch"],
            row["expires_at"],
            run_id="r",
        )
        await lose_key(self.leases[0].key_for_session("s"))
        other = await self.leases[1].acquire("s", "B", "preclaim", 30)
        with self.assertRaises(SessionConflictError):
            await self.stores[1].claim_next(
                other,
                30,
                execution_operation("blocked", ClaimExecutionRequest(other, 30)),
            )
        self.clock.advance(10)
        self.assertEqual((await asyncio.wait_for(task, 10)).attempts[0].status, "retry")
        self.assertEqual(
            await self.clients[1].exists(self.leases[1].key_for_session("s")), 1
        )
        await self.leases[1].release(other)
        second = self.worker(1)
        self.assertEqual((await second.run_once()).attempts[0].status, "completed")
        record = await self.stores[1].load_execution("s", "r")
        self.assertEqual(
            (record.epoch, record.attempt, record.checkpoint.dispatches),
            (old.epoch + 1, 2, 2),
        )
        for grant in (
            old,
            replace(old, owner_id="foreign"),
            replace(old, claim_id="foreign"),
        ):
            req = ExecutionStepRequest(grant, DispatchModel(), (), 1, 1)
            with self.assertRaises(SessionConflictError):
                await self.stores[0].commit_step(
                    grant,
                    execution_operation(uuid4().hex, req),
                    DispatchModel(),
                    (),
                    expected_session_version=1,
                    expected_revision=1,
                )

    async def test_unknown_real_tool_commit_is_queried_without_tool_reexecution(self):
        await self.accept()
        proxy = LostReplyPool(self.pool)
        proxy.lose = False
        original = PostgresExecutionStore(proxy)

        class Store:
            def __getattr__(self, name):
                return getattr(original, name)

            def bind_writer(self, grant):
                bound = original.bind_writer(grant)

                class Writer:
                    async def commit_step(
                        self, operation, command, mutations, **kwargs
                    ):
                        if isinstance(command, CommitToolResults):
                            proxy.lose = True
                        return await bound.commit_step(
                            operation, command, mutations, **kwargs
                        )

                return Writer()

        calls = []

        async def execute(arguments, context):
            calls.append(context.call.id)
            return ToolOutput("committed")

        tool = FunctionTool(ToolDefinition("do", "do", {"type": "object"}), execute)
        provider = FakeProvider(
            [
                [ToolCall("call", "do", {}), Finish("tool_calls")],
                [TextDelta("done"), Finish()],
            ]
        )
        report = await self.worker(0, provider, store=Store(), tools=(tool,)).run_once()
        self.assertEqual(report.attempts[0].status, "completed")
        self.assertEqual(calls, ["call"])
        self.assertFalse(proxy.lose)

    async def test_excluded_busy_head_does_not_starve_ready_session(self):
        await self.accept("first")
        await self.accept("second")
        lease = await self.leases[1].acquire("first", "other", "claim", 30)
        worker = self.worker(0, settings=RecoveryWorkerSettings(scan_limit=1))
        try:
            self.assertEqual((await worker.run_once()).attempts[0].status, "busy")
            report = await worker.run_once()
            self.assertEqual(
                (report.attempts[0].session_id, report.attempts[0].status),
                ("second", "completed"),
            )
        finally:
            await self.leases[1].release(lease)

    async def test_hard_process_exit_mid_stream_is_taken_over_without_partial_message(
        self,
    ):
        await self.accept()
        environment = dict(os.environ)
        environment.update(
            PI_AGENT_RECOVERY_SCHEMA=self.schema, PI_AGENT_RECOVERY_PREFIX=self.prefix
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(Path(__file__).with_name("recovery_process.py")),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.processes.append(process)
        line = await asyncio.wait_for(process.stdout.readline(), 20)
        self.assertEqual(json.loads(line)["barrier"], "partial_stream")
        process.kill()
        await asyncio.wait_for(process.wait(), 10)
        checkpoint = await self.stores[0].load_execution("s", "r")
        self.assertEqual(
            (
                checkpoint.state,
                checkpoint.checkpoint.phase,
                checkpoint.checkpoint.dispatches,
            ),
            ("running", "awaiting_model", 1),
        )
        self.assertEqual(
            [m.content for m in (await self.store.load("s")).messages], ["hello"]
        )
        # Advance only task-owned server state, without an arbitrary TTL sleep.
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE sessions SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE session_id=$1",
                "s",
            )
        await self.clients[0].pexpireat(self.leases[0].key_for_session("s"), 1)
        provider = FakeProvider([[TextDelta("taken over"), Finish()]])
        report = await self.worker(1, provider).run_once()
        self.assertEqual(report.attempts[0].status, "completed")
        record = await self.stores[1].load_execution("s", "r")
        self.assertEqual(
            (record.epoch, record.attempt, record.checkpoint.dispatches), (2, 2, 2)
        )
        self.assertEqual(
            [m.content for m in (await self.store.load("s")).messages],
            ["hello", "taken over"],
        )
