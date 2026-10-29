from __future__ import annotations

import asyncio
import unittest

from pi_agent_py import (
    Agent,
    AttemptFailure,
    CapabilityRoutePolicy,
    DefaultRetryPolicy,
    DirectModelRuntime,
    FakeProvider,
    Finish,
    FullJitterBackoff,
    InMemoryEventSink,
    Message,
    ProviderAuthenticationError,
    ProviderCandidate,
    ProviderExhaustedError,
    ProviderRateLimitError,
    ProviderRequestContext,
    ProviderState,
    ProviderTimeoutError,
    ResilientModelRuntime,
    TextDelta,
    ToolDefinition,
)


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


def _context() -> ProviderRequestContext:
    return ProviderRequestContext(
        request_id="request-1",
        attempt_id="attempt-1",
        purpose="agent_turn",
        request_number=1,
        run_id="run-1",
        turn_id="run-1:turn:1",
    )


async def _collect(runtime: ResilientModelRuntime, messages=(Message("user", "hi"),), tools=()):
    return [
        event
        async for event in runtime.stream(
            messages,
            tools,
            context=_context(),
        )
    ]


class ResilientModelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_pre_output_failure_with_new_attempt_identity(self) -> None:
        provider = FakeProvider(
            [
                [ProviderTimeoutError("temporary timeout")],
                [TextDelta("ok"), Finish("stop")],
            ]
        )
        clock = _FakeTime()
        events = InMemoryEventSink()
        runtime = ResilientModelRuntime(
            [ProviderCandidate("primary", provider)],
            event_sink=events,
            backoff=FullJitterBackoff(
                base_delay_seconds=0.5,
                max_delay_seconds=0.5,
                random_uniform=lambda _low, high: high,
            ),
            sleep=clock.sleep,
            clock=clock.clock,
        )

        result = await _collect(runtime)

        self.assertEqual(result, [TextDelta("ok"), Finish("stop")])
        self.assertEqual(clock.delays, [0.5])
        self.assertEqual(len(provider.request_contexts), 2)
        first, second = provider.request_contexts
        self.assertEqual(first.request_id, second.request_id)
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        self.assertEqual([first.attempt, second.attempt], [1, 2])
        self.assertEqual(
            [event.kind for event in events.events],
            [
                "provider_route_selected",
                "provider_attempt_started",
                "provider_attempt_failed",
                "provider_retry_scheduled",
                "provider_attempt_started",
                "provider_attempt_succeeded",
            ],
        )

    async def test_does_not_retry_after_any_stream_event_was_forwarded(self) -> None:
        provider = FakeProvider(
            [
                [TextDelta("partial"), ProviderTimeoutError("stream broke")],
                [TextDelta("duplicate"), Finish("stop")],
            ]
        )
        events = InMemoryEventSink()
        runtime = ResilientModelRuntime(
            [ProviderCandidate("primary", provider)],
            event_sink=events,
        )

        received = []
        with self.assertRaisesRegex(ProviderTimeoutError, "stream broke"):
            async for event in runtime.stream(
                (Message("user", "hi"),),
                context=_context(),
            ):
                received.append(event)

        self.assertEqual(received, [TextDelta("partial")])
        self.assertEqual(len(provider.requests), 1)
        failed = next(
            event for event in events.events if event.kind == "provider_attempt_failed"
        )
        self.assertTrue(failed.payload["output_started"])

    async def test_falls_back_after_primary_retry_budget_is_exhausted(self) -> None:
        primary = FakeProvider(
            [
                [ProviderTimeoutError("primary-1")],
                [ProviderTimeoutError("primary-2")],
            ]
        )
        fallback = FakeProvider([[TextDelta("fallback"), Finish("stop")]])
        clock = _FakeTime()
        events = InMemoryEventSink()
        runtime = ResilientModelRuntime(
            [
                ProviderCandidate("primary", primary),
                ProviderCandidate("fallback", fallback),
            ],
            event_sink=events,
            backoff=FullJitterBackoff(
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
            sleep=clock.sleep,
            clock=clock.clock,
        )

        result = await _collect(runtime)

        self.assertEqual(result, [TextDelta("fallback"), Finish("stop")])
        self.assertEqual(len(primary.requests), 2)
        self.assertEqual(len(fallback.requests), 1)
        selected = next(
            event
            for event in events.events
            if event.kind == "provider_fallback_selected"
        )
        self.assertEqual(selected.payload["from_provider"], "primary")
        self.assertEqual(selected.payload["to_provider"], "fallback")

    async def test_non_retryable_failure_does_not_fallback(self) -> None:
        primary = FakeProvider([[ProviderAuthenticationError("bad key")]])
        fallback = FakeProvider([[TextDelta("must not run"), Finish("stop")]])
        runtime = ResilientModelRuntime(
            [
                ProviderCandidate("primary", primary),
                ProviderCandidate("fallback", fallback),
            ]
        )

        with self.assertRaisesRegex(ProviderAuthenticationError, "bad key"):
            await _collect(runtime)

        self.assertEqual(len(primary.requests), 1)
        self.assertEqual(fallback.requests, [])

    async def test_provider_owned_state_pins_the_compatible_candidate(self) -> None:
        incompatible = FakeProvider([[TextDelta("wrong"), Finish("stop")]])
        compatible = FakeProvider([[TextDelta("continued"), Finish("stop")]])
        events = InMemoryEventSink()
        runtime = ResilientModelRuntime(
            [
                ProviderCandidate("other", incompatible),
                ProviderCandidate(
                    "timicc",
                    compatible,
                    state_owners=frozenset({"timicc-responses"}),
                ),
            ],
            route_policy=CapabilityRoutePolicy(),
            event_sink=events,
        )
        history = (
            Message("user", "use a tool"),
            Message(
                "assistant",
                "",
                provider_state=(
                    ProviderState("timicc-responses", {"type": "reasoning"}),
                ),
            ),
        )

        result = await _collect(runtime, messages=history)

        self.assertEqual(result, [TextDelta("continued"), Finish("stop")])
        self.assertEqual(incompatible.requests, [])
        route = events.events[0]
        self.assertEqual(route.payload["candidate_names"], ("timicc",))

    async def test_route_skips_candidate_without_tool_capability(self) -> None:
        text_only = FakeProvider([[TextDelta("wrong"), Finish("stop")]])
        tool_provider = FakeProvider([[TextDelta("right"), Finish("stop")]])
        runtime = ResilientModelRuntime(
            [
                ProviderCandidate("text", text_only, supports_tools=False),
                ProviderCandidate("tools", tool_provider),
            ]
        )
        definition = ToolDefinition(
            name="lookup",
            description="Look something up.",
            input_schema={"type": "object"},
        )

        result = await _collect(runtime, tools=(definition,))

        self.assertEqual(result, [TextDelta("right"), Finish("stop")])
        self.assertEqual(text_only.requests, [])

    async def test_exhaustion_retains_only_secret_free_attempt_summaries(self) -> None:
        provider = FakeProvider(
            [
                [ProviderTimeoutError("secret first body")],
                [ProviderTimeoutError("secret second body")],
            ]
        )
        clock = _FakeTime()
        runtime = ResilientModelRuntime(
            [ProviderCandidate("primary", provider)],
            backoff=FullJitterBackoff(
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
            sleep=clock.sleep,
            clock=clock.clock,
        )

        with self.assertRaises(ProviderExhaustedError) as raised:
            await _collect(runtime)

        self.assertEqual(len(raised.exception.attempts), 2)
        self.assertNotIn("secret", str(raised.exception))
        self.assertIn("primary:ProviderTimeoutError", str(raised.exception))

    async def test_total_attempt_budget_stops_before_fallback(self) -> None:
        primary = FakeProvider(
            [
                [ProviderTimeoutError("primary-1")],
                [ProviderTimeoutError("primary-2")],
            ]
        )
        fallback = FakeProvider([[TextDelta("must not run"), Finish("stop")]])
        clock = _FakeTime()
        runtime = ResilientModelRuntime(
            [
                ProviderCandidate("primary", primary),
                ProviderCandidate("fallback", fallback),
            ],
            max_total_attempts=2,
            backoff=FullJitterBackoff(
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
            sleep=clock.sleep,
            clock=clock.clock,
        )

        with self.assertRaises(ProviderExhaustedError):
            await _collect(runtime)

        self.assertEqual(len(primary.requests), 2)
        self.assertEqual(fallback.requests, [])

    async def test_retry_after_is_clamped_and_run_settles_once(self) -> None:
        provider = FakeProvider(
            [
                [
                    ProviderRateLimitError(
                        "slow down",
                        retry_after_seconds=30,
                    )
                ],
                [TextDelta("ok"), Finish("stop")],
            ]
        )
        clock = _FakeTime()
        events = InMemoryEventSink()
        runtime = ResilientModelRuntime(
            [ProviderCandidate("primary", provider)],
            event_sink=events,
            backoff=FullJitterBackoff(
                base_delay_seconds=0.25,
                max_delay_seconds=2,
            ),
            sleep=clock.sleep,
            clock=clock.clock,
        )
        agent = Agent(runtime, event_sink=events)

        result = await agent.run("hello")

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(clock.delays, [2.0])
        self.assertEqual(
            sum(event.kind == "run_settled" for event in events.events),
            1,
        )

    async def test_cancellation_during_backoff_prevents_another_attempt(self) -> None:
        provider = FakeProvider(
            [
                [ProviderTimeoutError("retry me")],
                [TextDelta("must not run"), Finish("stop")],
            ]
        )
        sleeping = asyncio.Event()

        async def block_sleep(_delay: float) -> None:
            sleeping.set()
            await asyncio.Event().wait()

        runtime = ResilientModelRuntime(
            [ProviderCandidate("primary", provider)],
            backoff=FullJitterBackoff(
                base_delay_seconds=0.1,
                max_delay_seconds=0.1,
            ),
            sleep=block_sleep,
        )
        task = asyncio.create_task(_collect(runtime))
        await sleeping.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(len(provider.requests), 1)


class DirectModelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_traces_one_attempt_and_preserves_original_error(self) -> None:
        provider = FakeProvider([[ProviderTimeoutError("direct timeout")]])
        events = InMemoryEventSink()
        runtime = DirectModelRuntime(
            provider,
            name="primary",
            event_sink=events,
        )

        with self.assertRaisesRegex(ProviderTimeoutError, "direct timeout"):
            async for _event in runtime.stream(
                (Message("user", "hi"),),
                context=_context(),
            ):
                pass

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            [event.kind for event in events.events],
            [
                "provider_route_selected",
                "provider_attempt_started",
                "provider_attempt_failed",
            ],
        )
        failed = events.events[-1]
        self.assertEqual(failed.envelope.attempt_id, "attempt-1")
        self.assertEqual(failed.payload["error"], "ProviderTimeoutError")


class RetryPolicyTests(unittest.TestCase):
    def test_default_policy_is_fail_closed_after_output(self) -> None:
        decision = DefaultRetryPolicy().decide(
            AttemptFailure(
                provider_name="primary",
                error=ProviderTimeoutError("timeout"),
                retryable=True,
                output_started=True,
                provider_attempt=1,
                total_attempt=1,
                elapsed_seconds=0,
            )
        )
        self.assertEqual(decision.action, "fail")


if __name__ == "__main__":
    unittest.main()
