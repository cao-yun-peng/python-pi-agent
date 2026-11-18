from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal

from pi_agent_py.api import (
    AgentEvent,
    AgentRuntimeBuilder,
    EventFilter,
    EventHub,
    Finish,
    LoggingTelemetrySink,
    ModelPrice,
    ModelProtocolError,
    PricingTable,
    TelemetryEvent,
    TextDelta,
    TokenUsage,
    SessionPoolPolicy,
)
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled


class EventHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_overflow_and_close(self) -> None:
        hub = EventHub()
        oldest = hub.subscribe(max_buffer=1, overflow="drop_oldest")
        newest = hub.subscribe(max_buffer=1, overflow="drop_newest")
        closed = hub.subscribe(max_buffer=1, overflow="close")
        filtered = hub.subscribe(EventFilter(kinds=frozenset({"run_settled"})))
        first = AgentEvent("run_started", "live")
        second = AgentEvent("run_settled", "live")
        hub.emit(first)
        hub.emit(second)
        hub.emit(second)
        await hub.aclose()
        self.assertEqual([event async for event in oldest], [second])
        self.assertEqual([event async for event in newest], [first])
        self.assertEqual([event async for event in closed], [first])
        self.assertEqual([event async for event in filtered], [second, second])
        self.assertEqual(
            (oldest.dropped_count, newest.dropped_count, closed.dropped_count),
            (2, 2, 1),
        )


class RunObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_streams_and_targeted_abort(self) -> None:
        provider = FakeProvider(
            [
                [TextDelta("partial"), WaitUntilCancelled()],
                [TextDelta("second session"), Finish()],
                [TextDelta("next run"), Finish()],
            ]
        )
        runtime = await AgentRuntimeBuilder().with_provider(provider).build()
        async with (
            runtime,
            await runtime.open_session("one") as one,
            await runtime.open_session("two") as two,
        ):
            active = await one.submit("blocked")
            await provider.blocked.wait()
            remove = await one.follow_up("cancel this queued run")
            keep = await one.follow_up("run this later")
            other = await two.submit("this can finish now")
            await remove.abort()
            self.assertEqual((await other.result()).message.content, "second session")
            await active.abort()
            self.assertEqual((await keep.result()).message.content, "next run")
            for handle, session_id, terminal in (
                (active, "one", "run_cancelled"),
                (remove, "one", "run_cancelled"),
                (keep, "one", "run_completed"),
                (other, "two", "run_completed"),
            ):
                events = [event async for event in handle.events()]
                self.assertEqual(
                    {event.envelope.run_id for event in events}, {handle.run_id}
                )
                self.assertEqual(
                    {event.envelope.session_id for event in events}, {session_id}
                )
                if handle is not remove:
                    self.assertEqual(events[0].kind, "run_started")
                self.assertEqual(
                    [event.kind for event in events[-2:]], [terminal, "run_settled"]
                )
                self.assertEqual([event async for event in handle.events()], [])
            for handle in (active, remove):
                with self.assertRaises(asyncio.CancelledError):
                    await handle.result()
            self.assertNotIn(
                "cancel this queued run",
                [m.content for m in (await one.snapshot()).messages],
            )

    async def test_slow_stream_does_not_block_others_or_lose_settled(self) -> None:
        provider = FakeProvider([[TextDelta("x")] * 12 + [Finish()]])
        runtime = await AgentRuntimeBuilder().with_provider(provider).build()
        async with runtime, await runtime.open_session("s") as session:
            all_events = runtime.event_hub.subscribe(EventFilter(session_id="s"))
            handle = await session.submit("hi", max_buffer=1)
            result = await handle.result()
            self.assertEqual(result.message.content, "x" * 12)
        full = [
            event
            async for event in all_events
            if event.envelope.run_id == handle.run_id
        ]
        bounded = [event.kind async for event in handle.events()]
        self.assertEqual(bounded, ["run_started", "run_settled"])
        self.assertEqual(handle.events().dropped_count, len(full) - 2)
        self.assertEqual(sum(event.kind == "text_delta" for event in full), 12)

    async def test_waiter_cancellation_and_stream_close_leave_run_alive(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        runtime = await AgentRuntimeBuilder().with_provider(provider).build()
        async with runtime, await runtime.open_session("s") as session:
            handle = await session.submit("hi")
            await provider.blocked.wait()
            self.assertEqual((await anext(handle.events())).kind, "run_started")
            await handle.events().aclose()
            waiter = asyncio.create_task(handle.result())
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(session.active_run_id, handle.run_id)
            self.assertEqual([event async for event in handle.events()], [])
            await handle.abort()

    async def test_close_settles_queued_handles_and_ends_hub_subscriptions(
        self,
    ) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        runtime = (
            await AgentRuntimeBuilder()
            .with_provider(provider)
            .with_graceful_shutdown(0)
            .build()
        )
        async with await runtime.open_session("s") as session:
            lifecycle = runtime.event_hub.subscribe(
                EventFilter(
                    channels=frozenset({"capability"}),
                    kinds=frozenset({"runtime_closed"}),
                )
            )
            active = await session.submit("active")
            await provider.blocked.wait()
            queued = await session.follow_up("queued")
            await runtime.aclose()
            for handle in (active, queued):
                kinds = [event.kind async for event in handle.events()]
                self.assertEqual(kinds[-2:], ["run_cancelled", "run_settled"])
                with self.assertRaises(asyncio.CancelledError):
                    await handle.result()
            self.assertEqual(
                [event.kind async for event in lifecycle], ["runtime_closed"]
            )

    async def test_missing_prices_and_usage_survives_controller_eviction(self) -> None:
        usage = TokenUsage(4, 1, 5)
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(FakeProvider([[usage, Finish()], [usage, Finish()]]))
            .with_session_pool(SessionPoolPolicy(idle_ttl_seconds=0))
            .build()
        )
        async with runtime:
            async with await runtime.open_session("s") as original:
                handle = await original.submit("hello")
                await handle.result()
                (completed,) = [
                    event
                    async for event in handle.events()
                    if event.kind == "run_completed"
                ]
                self.assertIsNone(completed.payload["estimated_cost_usd"])
                self.assertEqual(completed.payload["unpriced_requests"], 1)
                await original.wait_until_idle()
            async with await runtime.open_session("s") as reopened:
                await reopened.run("again")
                self.assertEqual(reopened.usage.usage, TokenUsage(8, 2, 10))
                self.assertEqual(reopened.usage.unpriced_requests, 2)

    async def test_usage_counts_final_reports_and_failed_requests(self) -> None:
        usage = TokenUsage(100, 10, 110, 20)
        provider = FakeProvider(
            [
                [TokenUsage(5, 0, 5), usage, Finish()],
                [usage, TextDelta("missing finish")],
                [TokenUsage(7, 3, 10), Finish()],
            ]
        )
        pricing = PricingTable(
            {"demo": ModelPrice(Decimal("2"), Decimal("5"), Decimal("0.5"))}
        )
        runtime = (
            await AgentRuntimeBuilder()
            .with_provider(provider, model="demo")
            .with_pricing(pricing)
            .build()
        )
        async with runtime, await runtime.open_session("s") as session:
            first = await session.submit("hi")
            self.assertEqual((await first.result()).usage, usage)
            (completed,) = [
                event async for event in first.events() if event.kind == "run_completed"
            ]
            self.assertEqual(
                completed.payload["usage"],
                {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "cached_input_tokens": 20,
                },
            )
            self.assertEqual(
                Decimal(completed.payload["estimated_cost_usd"]), Decimal("0.00022")
            )
            failed = await session.submit("fail")
            with self.assertRaises(ModelProtocolError):
                await failed.result()
            failed_events = [event.kind async for event in failed.events()]
            self.assertEqual(failed_events[-2:], ["run_failed", "run_settled"])
            self.assertEqual(
                runtime.usage_ledger.for_run("s", failed.run_id).usage, usage
            )
            await runtime.run("other", "do not include in s")
            self.assertEqual(session.usage.usage, TokenUsage(200, 20, 220, 40))
            self.assertEqual(session.usage.estimated_cost_usd, Decimal("0.00044"))
            self.assertEqual(
                runtime.usage_ledger.for_session("other").usage, TokenUsage(7, 3, 10)
            )

    async def test_telemetry_is_content_free_owned_and_failure_isolated(self) -> None:
        class Sink:
            def __init__(self) -> None:
                self.events: list[TelemetryEvent] = []
                self.closed = False
                self.log = LoggingTelemetrySink()

            def on_event(self, event: TelemetryEvent) -> None:
                self.events.append(event)
                self.log.on_event(event)
                if event.kind == "text_delta":
                    raise RuntimeError("private diagnostic")

            async def aclose(self) -> None:
                self.closed = True

        sink = Sink()
        with self.assertLogs("pi_agent_py.telemetry", "INFO") as records:
            runtime = await (
                AgentRuntimeBuilder()
                .with_provider(FakeProvider([[TextDelta("private output"), Finish()]]))
                .with_telemetry_sink(sink, ownership="owned")
                .build()
            )
            async with runtime:
                result = await runtime.run("s", "private prompt")
        self.assertEqual(result.message.content, "private output")
        self.assertTrue(sink.closed)
        self.assertEqual(sink.events[-1].kind, "runtime_closed")
        self.assertTrue(all(not hasattr(event, "payload") for event in sink.events))
        logged = repr([record.pi_agent for record in records.records])
        for private in (
            "private prompt",
            "private output",
            "private diagnostic",
            "arguments",
        ):
            self.assertNotIn(private, logged)
        self.assertEqual(
            [failure.event_kind for failure in runtime.event_sink_failures],
            ["text_delta"],
        )
