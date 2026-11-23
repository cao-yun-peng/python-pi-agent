"""Offline admission, retry, and policy contracts with no real waiting."""

import asyncio
import unittest

from pi_agent_py.application.evaluation.policy import EvaluationPolicy, LimitedProvider, SharedRequestLimiter
from pi_agent_py.domain import Finish, Message, ProviderRequestContext
from pi_agent_py.errors import ProviderRateLimitError
from pi_agent_py.ports import ProviderCandidate
from pi_agent_py.providers.runtime import DefaultRetryPolicy, FullJitterBackoff, ResilientModelRuntime
from pi_agent_py.testing import FakeProvider


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


def context():
    return ProviderRequestContext("request", "attempt", "agent_turn", 1, run_id="run", turn_id="turn")


class PolicyTests(unittest.TestCase):
    def test_strict_configuration_and_roundtrip(self):
        policy = EvaluationPolicy.parse({"max_requests": 40, "min_request_interval_seconds": 0})
        self.assertEqual(EvaluationPolicy.parse(policy.record()), policy)
        for values in ({"max_requests": True}, {"timeout_seconds": float("nan")},
                       {"retry_attempts": 0}, {"retry_base_seconds": 80}, {"unknown": 1}):
            with self.subTest(values=values), self.assertRaises((ValueError, TypeError)):
                EvaluationPolicy.parse(values)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_consumers_and_retries_share_admission(self):
        time = FakeTime()
        limiter = SharedRequestLimiter(30, clock=time.clock, sleep=time.sleep)
        raw = FakeProvider([[ProviderRateLimitError("limited")], [Finish()], [Finish()]])
        limited = LimitedProvider(raw, limiter)
        runtime = ResilientModelRuntime((ProviderCandidate("fake", limited),),
            retry_policy=DefaultRetryPolicy(max_attempts_per_provider=2),
            backoff=FullJitterBackoff(base_delay_seconds=0, max_delay_seconds=0),
            clock=time.clock, sleep=time.sleep, max_elapsed_seconds=120)
        self.assertEqual([e async for e in runtime.stream([Message("user", "a")], context=context())], [Finish()])
        other = LimitedProvider(raw, limiter)
        self.assertEqual([e async for e in other.stream([Message("user", "b")], context=context())], [Finish()])
        self.assertEqual(time.now, 60)
        self.assertEqual(limiter.waited_seconds, 60)
        self.assertEqual(len(raw.requests), 3)
        self.assertNotEqual(raw.request_contexts[0].attempt_id, raw.request_contexts[1].attempt_id)

    async def test_cancelled_wait_releases_lock_without_spending_slot(self):
        started = asyncio.Event()
        async def wait(seconds):
            started.set()
            await asyncio.Future()
        time = FakeTime()
        limiter = SharedRequestLimiter(30, clock=time.clock, sleep=wait)
        await limiter.acquire()
        waiting = asyncio.create_task(limiter.acquire())
        await started.wait()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        time.now = 30
        await limiter.acquire()
