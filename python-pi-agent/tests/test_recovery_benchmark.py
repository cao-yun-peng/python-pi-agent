"""Offline deterministic benchmark contracts; no Docker, PG, Redis or real model."""

import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pi_agent_py.application.benchmark_report import (
    BenchmarkSample,
    BenchmarkSettings,
    distribution,
    summarize,
)
from pi_agent_py.application.benchmark_cli import main, write_report
from pi_agent_py.application.recovery_benchmark import (
    BenchmarkPeer,
    BenchmarkTrace,
    run_batch,
    run_benchmark,
)
from pi_agent_py.domain import AgentEvent
from pi_agent_py.observability.events import make_agent_event
from pi_agent_py.storage.lease import InMemoryLeaseManager
from pi_agent_py.testing_clock import ManualLeaseClock
from execution_runtime_fixture import ExecutionMemory
from lease_fakes import pump_until


class BenchmarkReportTests(unittest.TestCase):
    def test_settings_validate_before_starting_any_io(self):
        for values in (
            {"samples": 0},
            {"workers": True},
            {"warmup": -1},
            {"timeout": float("nan")},
            {"scenarios": ()},
            {"scenarios": ("text", "text")},
            {"scenarios": ("unknown",)},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BenchmarkSettings(**values)

    def test_nearest_rank_empty_and_small_population(self):
        self.assertEqual(distribution([])["p99_ms"], None)
        result = distribution([4, 1, 2, 3])
        self.assertEqual(
            (result["count"], result["p50_ms"], result["p95_ms"], result["p99_ms"]),
            (4, 2, 4, 4),
        )
        with self.assertRaises(ValueError):
            distribution([float("inf")])

    def test_failures_and_incomplete_samples_are_not_success(self):
        sample = BenchmarkSample(
            "s",
            "r",
            "text",
            "running",
            None,
            1,
            1,
            1,
            1,
            0,
            1,
            0,
            1,
            False,
            None,
            None,
            ("A",),
        )
        report = summarize(
            "text", (sample,), 2, {"busy": 3, "claimed": 1}, timed_out=True
        )
        self.assertEqual(report["completed"], 0)
        self.assertEqual(report["completed_runs_per_second"], 0)
        self.assertEqual(report["conflict_rate"], 0.75)
        self.assertEqual(report["terminal_failure_categories"], {"running": 1})

    def test_report_write_is_complete_utf8_json(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nested" / "report.json"
            write_report(output, {"status": "valid", "message": "可重复"})
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["message"], "可重复"
            )
            self.assertEqual(
                [path.name for path in output.parent.iterdir()], ["report.json"]
            )

    def test_cli_failure_redacts_raw_exception_and_returns_nonzero(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                "pi_agent_py.application.benchmark_cli.benchmark",
                side_effect=RuntimeError("password=DO_NOT_LOG"),
            ),
        ):
            output = Path(folder) / "error.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = main(["--samples", "1", "--output", str(output)])
            self.assertEqual(result, 1)
            content = output.read_text(encoding="utf-8")
            self.assertEqual(json.loads(content)["error_type"], "RuntimeError")
            self.assertNotIn("DO_NOT_LOG", content + stdout.getvalue())


class BenchmarkRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualLeaseClock()
        self.trace = BenchmarkTrace(self.clock)
        trace = self.trace

        class Store(ExecutionMemory):
            def bind_writer(self, grant):
                return trace.bind_writer(grant, super().bind_writer(grant))

        self.store = Store(self.clock)
        self.leases = InMemoryLeaseManager(clock=self.clock)
        peer = BenchmarkPeer(self.store, self.store, self.leases)
        self.peers = (
            peer,
            peer,
        )  # Only this offline fixture shares in-memory authority.
        self.settings = BenchmarkSettings(samples=2, warmup=0)

    async def asyncTearDown(self):
        self.assertEqual(self.clock.pending_sleeps, 0)
        self.assertEqual(self.clock.pending_timeouts, 0)
        self.assertFalse(
            any(
                task.get_name().startswith(("recovery-", "lease-", "agent-run:"))
                for task in asyncio.all_tasks()
            )
        )

    async def finish(self, awaitable):
        task = asyncio.create_task(awaitable)
        try:
            for index in range(3000):
                if task.done():
                    return await task
                await asyncio.sleep(0)
                if index % 10 == 0:
                    self.clock.advance(0.025)
            self.fail("benchmark did not settle with bounded manual scheduler")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_four_scenarios_reuse_formal_loop_and_validate_durable_evidence(self):
        result = await self.finish(
            run_benchmark(
                self.peers, self.trace, self.settings, {"code_commit": "offline"}
            )
        )
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["paid_model_calls"], 0)
        batches = result["batches"]
        self.assertEqual(
            [batch["scenario"] for batch in batches],
            ["text", "partial_stream", "final_assistant", "tool_tail"],
        )
        self.assertEqual([batch["correct"] for batch in batches], [2, 2, 2, 2])
        self.assertEqual(batches[0]["recovery_latency"]["count"], 0)
        self.assertEqual(
            [batch["recovery_latency"]["count"] for batch in batches[1:]], [2, 2, 2]
        )
        self.assertEqual([s["model_requests"] for s in batches[2]["samples"]], [1, 1])
        self.assertEqual([s["unknown_results"] for s in batches[3]["samples"]], [1, 1])
        self.assertEqual([s["tool_executions"] for s in batches[3]["samples"]], [1, 1])

    async def test_warmup_is_reported_separately_and_not_in_measured_sample_count(self):
        settings = replace(self.settings, warmup=1, scenarios=("text",))
        result = await self.finish(run_benchmark(self.peers, self.trace, settings, {}))
        self.assertEqual(result["warmup"]["sample_count"], 1)
        self.assertEqual(result["batches"][0]["sample_count"], 2)
        with patch(
            "pi_agent_py.application.recovery_benchmark.run_batch",
            return_value={
                "correct": 1,
                "timed_out": False,
                "cleanup_success": False,
            },
        ) as batch:
            result = await run_benchmark(self.peers, self.trace, settings, {})
            self.assertEqual(result["status"], "invalid")
            self.assertEqual(batch.await_count, 1)

    async def test_timeout_retains_incomplete_samples_and_invalidates_report(self):
        async def occupied(*args, **kwargs):
            return None

        self.leases.acquire = occupied
        settings = replace(self.settings, timeout=1, scenarios=("text",))
        result = await self.finish(run_benchmark(self.peers, self.trace, settings, {}))
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(result["batches"][0]["timed_out"])
        self.assertEqual(
            result["batches"][0]["terminal_failure_categories"], {"queued": 2}
        )
        self.assertEqual(result["batches"][0]["recovery_latency"]["count"], 0)

    async def test_parent_cancel_joins_workers_heartbeats_and_releases_authority(self):
        from pi_agent_py.execution_domain import DispatchModel

        entered = asyncio.Event()
        original = self.store.commit_step

        async def blocked(grant, operation, command, mutations, **kwargs):
            if isinstance(command, DispatchModel):
                entered.set()
                await asyncio.Event().wait()
            return await original(grant, operation, command, mutations, **kwargs)

        self.store.commit_step = blocked
        task = asyncio.create_task(
            run_batch(self.peers, self.trace, self.settings, "text", 1)
        )
        try:
            await pump_until(entered.is_set)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            sid = next(iter(self.trace.observations))
            self.assertIsNone(self.store.authority(sid).grant)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_trace_copies_only_allowlisted_fields(self):
        self.trace.add("s", "r", "text")
        event: AgentEvent = make_agent_event(
            "recovery_failed",
            "capability",
            source="test",
            session_id="s",
            payload={
                "error_type": "SessionStorageError",
                "prompt": "private",
                "nonce": "secret",
                "epoch": {"password": "hidden"},
            },
        )
        self.trace.event("A", event)
        self.assertNotIn("private", json.dumps(self.trace.events))
        self.assertNotIn("secret", json.dumps(self.trace.events))
        self.assertNotIn("hidden", json.dumps(self.trace.events))

    async def test_repeated_cancel_waits_for_all_worker_closes(self):
        from pi_agent_py.runtime.recovery import SessionRecoveryWorker

        entered, release = asyncio.Event(), asyncio.Event()
        original = SessionRecoveryWorker.aclose
        closes = []

        async def close(worker):
            closes.append(worker)
            entered.set()
            await release.wait()
            await original(worker)

        with patch.object(SessionRecoveryWorker, "aclose", close):
            task = asyncio.create_task(
                run_batch(self.peers, self.trace, self.settings, "text", 1)
            )
            try:
                await pump_until(entered.is_set)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(len(closes), 2)
                self.assertTrue(all(worker.state == "closed" for worker in closes))
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_sessionless_scanner_failure_is_counted_without_private_details(self):
        event = make_agent_event(
            "recovery_scan_failed",
            "capability",
            source="test",
            payload={"error_type": "SessionStorageError", "dsn": "private"},
        )
        self.trace.event("A", event)
        self.assertEqual(self.trace.attempts["scan:SessionStorageError"], 1)
        self.assertNotIn("private", json.dumps(self.trace.events))


class DevelopmentEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / "scripts" / "distributed_dev.py"
        spec = importlib.util.spec_from_file_location("distributed_dev", path)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def test_environment_is_loopback_scoped_and_does_not_inherit_redis_restart(self):
        with patch.dict(
            "os.environ", {"PI_AGENT_REDIS_TEST_RESTART_CONTAINER": "unrelated"}
        ):
            selected = self.module.Environment("pi-agent-test", 55432, 56379)
            variables = selected.variables()
            self.assertNotIn("PI_AGENT_REDIS_TEST_RESTART_CONTAINER", variables)
            self.assertIn("@127.0.0.1:55432/", variables["PI_AGENT_POSTGRES_TEST_DSN"])
            self.assertEqual(
                selected.compose("down")[-3:], ["-p", "pi-agent-test", "down"]
            )
        with self.assertRaises(ValueError):
            self.module.Environment("unrelated-project")
        with self.assertRaises(ValueError):
            self.module.Environment(pg_port=56379)

    def test_compose_service_ports_health_and_restart_opt_in_match_entry(self):
        import yaml

        document = yaml.safe_load(self.module.COMPOSE.read_text(encoding="utf-8"))
        for service in document["services"].values():
            self.assertTrue(service["ports"][0].startswith("127.0.0.1:"))
            self.assertIn("healthcheck", service)
        self.assertEqual(
            document["services"]["redis"]["labels"]["pi-agent.test-redis-restart"],
            "true",
        )
        self.assertIn("pg-data", document["volumes"])
