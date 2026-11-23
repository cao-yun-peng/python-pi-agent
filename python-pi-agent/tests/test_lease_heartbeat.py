import asyncio
import json
import unittest

from pi_agent_py.errors import (
    LeaseLostError,
    LeaseTimeoutError,
    RuntimeStateError,
    SessionCommitUnknownError,
    SessionConflictError,
    SessionValidationError,
)
from pi_agent_py.execution_domain import ExecutionGrant
from pi_agent_py.observability.events import InMemoryEventSink
from pi_agent_py.runtime.lease_heartbeat import LeaseHeartbeat, LeaseHeartbeatSettings
from pi_agent_py.testing_clock import ManualLeaseClock
from lease_fakes import ControlledGrants, ControlledLeases, pump_until


class LeaseHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = ManualLeaseClock()
        self.trace = []
        self.leases = ControlledLeases(self.clock, self.trace)
        self.lease = await self.leases.acquire("s", "w", "c", 3)
        self.grant = ExecutionGrant(
            "s", "w", "c", 1, self.clock.unix_time() + 3, run_id="r"
        )
        self.grants = ControlledGrants(self.clock, self.grant, self.trace)
        self.settings = LeaseHeartbeatSettings(3, 0.5, 0.25, 0.25, 0.25, 0.1)
        self.events = InMemoryEventSink()
        self.guard = LeaseHeartbeat(
            self.leases,
            self.grants,
            self.lease,
            self.grant,
            settings=self.settings,
            clock=self.clock,
            event_sink=self.events,
        )
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.done = asyncio.Event()

    async def work(self, guard):
        guard.ensure_active()
        self.started.set()
        try:
            await self.done.wait()
            return 42
        finally:
            self.cancelled.set()

    async def start(self):
        task = asyncio.create_task(self.guard.run(self.work))
        await self.started.wait()
        await pump_until(lambda: self.clock.pending_sleeps == 1)
        return task

    def assert_closed(self):
        self.assertEqual(self.guard.state, "closed")
        self.assertEqual(self.clock.pending_sleeps, 0)
        self.assertEqual(self.clock.pending_timeouts, 0)
        self.assertFalse(
            any(
                t.get_name().startswith(
                    ("lease-work:", "lease-heartbeat:", "lease-cleanup:")
                )
                for t in asyncio.all_tasks()
            )
        )

    async def test_order_renewal_success_and_no_background_leak(self):
        task = await self.start()
        self.clock.advance(0.5)
        await pump_until(lambda: self.grants.renews == 2)
        self.assertEqual(
            self.trace[:4], ["redis-renew", "pg-renew", "redis-renew", "pg-renew"]
        )
        self.done.set()
        self.assertEqual(await task, 42)
        self.assertEqual(self.trace[-2:], ["pg-release", "redis-release"])
        self.assertEqual(self.events.events[0].kind, "lease_heartbeat_started")
        self.assertEqual(self.events.events[-1].kind, "lease_heartbeat_stopped")
        self.assert_closed()

    async def test_redis_clear_cancels_owned_work_and_closes_dispatch_gate(self):
        task = await self.start()
        self.leases.clear()
        self.clock.advance(0.5)
        with self.assertRaises(LeaseLostError) as caught:
            await task
        self.assertEqual(caught.exception.reason, "redis_lost")
        self.assertTrue(self.cancelled.is_set())
        with self.assertRaises(LeaseLostError):
            self.guard.ensure_active()
        self.assert_closed()

    async def test_startup_revalidates_before_work_can_dispatch(self):
        self.leases.clear()
        with self.assertRaises(LeaseLostError):
            await self.guard.run(self.work)
        self.assertFalse(self.started.is_set())
        self.assertEqual(self.grants.renews, 0)
        self.assert_closed()

    async def test_redis_timeout_stops_pg_renewal_and_joins_cancelled_io(self):
        task = await self.start()
        self.leases.block = True
        self.leases.renew_started.clear()
        self.clock.advance(0.5)
        await self.leases.renew_started.wait()
        self.clock.advance(0.25)
        with self.assertRaises(LeaseLostError) as caught:
            await task
        self.assertEqual(caught.exception.reason, "redis_timeout")
        self.assertEqual(self.grants.renews, 1)
        self.assertTrue(self.leases.renew_cancelled.is_set())
        self.assert_closed()

    async def test_pg_timeout_and_error_cancel_work(self):
        task = await self.start()
        self.grants.block = True
        self.grants.renew_started.clear()
        self.clock.advance(0.5)
        await self.grants.renew_started.wait()
        self.clock.advance(0.25)
        with self.assertRaises(LeaseLostError) as caught:
            await task
        self.assertEqual(caught.exception.reason, "grant_timeout")
        self.assert_closed()

    async def test_redis_and_grant_exceptions_are_observable_without_secrets(self):
        self.grants.failure = OSError("private token=do-not-log")
        with self.assertRaises(LeaseLostError) as caught:
            await self.guard.run(self.work)
        self.assertEqual(caught.exception.reason, "grant_error")
        self.assertIsInstance(caught.exception.__cause__, OSError)
        payload = json.dumps([e.payload for e in self.events.events])
        self.assertNotIn("do-not-log", payload)
        self.assertNotIn(self.lease.nonce, payload)
        self.assert_closed()

    async def test_parent_cancellation_and_concurrent_close_release_once(self):
        task = await self.start()
        await asyncio.gather(self.guard.aclose(), self.guard.aclose())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.grants.releases, 1)
        await self.guard.aclose()
        self.assert_closed()

    async def test_redis_error_fails_closed_before_pg_renewal(self):
        self.leases.failure = OSError("private")
        with self.assertRaises(LeaseLostError) as caught:
            await self.guard.run(self.work)
        self.assertEqual(caught.exception.reason, "redis_error")
        self.assertEqual(self.grants.renews, 0)
        self.assert_closed()

    async def test_pg_rejection_cancels_work(self):
        task = await self.start()
        self.grants.failure = SessionConflictError("newer epoch")
        self.clock.advance(0.5)
        with self.assertRaises(LeaseLostError) as caught:
            await task
        self.assertEqual(caught.exception.reason, "grant_lost")
        self.assertTrue(self.cancelled.is_set())
        self.assert_closed()

    async def test_cancellation_during_initial_renewal_never_dispatches_work(self):
        self.leases.block = True
        task = asyncio.create_task(self.guard.run(self.work))
        await self.leases.renew_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.started.is_set())
        self.assertTrue(self.leases.renew_cancelled.is_set())
        self.assert_closed()

    async def test_finalizer_commit_unknown_is_preserved_for_operation_query(self):
        async def work(_):
            return 42

        async def finalize(result, grant):
            raise SessionCommitUnknownError("s", "finish")

        with self.assertRaises(SessionCommitUnknownError):
            await self.guard.run(work, finalize=finalize)
        self.assertIsNone(self.guard.lost_reason)
        self.assert_closed()

    async def test_finalizer_timeout_joins_callback_before_cleanup(self):
        finalizing, cancelled = asyncio.Event(), asyncio.Event()

        async def work(_):
            return 42

        async def finalize(result, grant):
            finalizing.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(self.guard.run(work, finalize=finalize))
        await finalizing.wait()
        self.clock.advance(0.25)
        with self.assertRaises(LeaseTimeoutError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assert_closed()

    async def test_repeated_parent_cancellation_cannot_orphan_cleanup(self):
        self.grants.release_gate = asyncio.Event()
        task = await self.start()
        task.cancel()
        await self.grants.release_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.grants.release_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.grants.releases, 1)
        self.assert_closed()

    async def test_cleanup_failure_does_not_erase_success(self):
        self.grants.release_failure = OSError("private cleanup failure")
        task = await self.start()
        self.done.set()
        self.assertEqual(await task, 42)
        self.assertEqual(self.guard.cleanup_failures[0].stage, "grant")
        self.assertEqual(self.trace[-1], "redis-release")
        self.assert_closed()

    async def test_cleanup_timeout_still_attempts_redis_release(self):
        self.grants.release_gate = asyncio.Event()
        task = await self.start()
        self.done.set()
        await self.grants.release_started.wait()
        self.clock.advance(0.25)
        self.assertEqual(await task, 42)
        self.assertEqual(self.guard.cleanup_failures[0].error_type, "LeaseTimeoutError")
        self.assertEqual(self.trace[-1], "redis-release")
        self.assert_closed()

    async def test_business_error_is_not_wrapped_in_exception_group(self):
        async def failed(guard):
            raise ValueError("business failure")

        with self.assertRaisesRegex(ValueError, "business failure"):
            await self.guard.run(failed)
        self.assertIsNone(self.guard.lost_reason)
        self.assert_closed()

    async def test_ignored_work_cancellation_cannot_return_false_success(self):
        async def stubborn(guard):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "pretend success"

        task = asyncio.create_task(self.guard.run(stubborn))
        await self.started.wait()
        await pump_until(lambda: self.clock.pending_sleeps == 1)
        self.leases.clear()
        self.clock.advance(0.5)
        with self.assertRaises(LeaseLostError):
            await task
        self.assert_closed()

    async def test_deadline_exceeded_does_not_revive_expired_scope(self):
        task = await self.start()
        self.clock.advance(4)
        self.done.set()
        with self.assertRaises(LeaseLostError):
            await task
        self.assertEqual(self.grants.renews, 1)
        self.assert_closed()

    async def test_finalize_runs_after_heartbeat_join_and_before_release(self):
        async def finalize(result, grant):
            self.assertEqual(result, 42)
            self.assertEqual(self.clock.pending_sleeps, 0)
            self.assertEqual(self.grants.releases, 0)
            self.grants.authority = self.grants.authority.__class__("s", 1)

        task = asyncio.create_task(self.guard.run(self.work, finalize=finalize))
        await self.started.wait()
        self.done.set()
        self.assertEqual(await task, 42)
        self.assertIsNone(self.guard.lost_reason)
        self.assert_closed()

    async def test_unstarted_close_has_no_worker_and_scope_is_one_shot(self):
        self.assertEqual(self.trace, [])
        await self.guard.aclose()
        self.assertEqual(self.trace, ["pg-release", "redis-release"])
        with self.assertRaises(RuntimeStateError):
            await self.guard.run(self.work)
        self.assert_closed()

    async def test_observer_failure_cannot_change_work_outcome(self):
        class BadSink:
            def emit(self, event):
                raise RuntimeError("observer")

        guard = LeaseHeartbeat(
            self.leases,
            self.grants,
            self.lease,
            self.grant,
            settings=self.settings,
            clock=self.clock,
            event_sink=BadSink(),
        )

        async def work(_):
            return 7

        self.assertEqual(await guard.run(work), 7)

    async def test_invalid_budgets_and_identity_fail_without_starting_tasks(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(SessionValidationError):
                LeaseHeartbeatSettings(heartbeat_interval=value)
        with self.assertRaises(SessionValidationError):
            LeaseHeartbeatSettings(ttl_seconds=1)
