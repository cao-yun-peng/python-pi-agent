"""B2 contracts exercised through the stable application facade."""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import fields
from decimal import Decimal
from typing import cast

from pi_agent_py.api import (
    AgentComponents,
    AgentEvent,
    AgentEventEnvelope,
    AgentRuntimeBuilder,
    CallbackEventSink,
    ConfigurationError,
    ContextPlan,
    Finish,
    FunctionTool,
    InMemorySessionStore,
    JSONObject,
    JSONValue,
    ModelPrice,
    PluginAPI,
    PricingTable,
    ProviderRequestContext,
    QueuedControl,
    RedactingEventSink,
    RuntimeBuildError,
    RuntimeStateError,
    SessionOpenRequest,
    SessionPoolPolicy,
    SessionSpec,
    SessionSpecResolver,
    StaticSessionSpecResolver,
    TelemetryEvent,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    serialize_agent_event,
)
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled


def _tool(name: str) -> FunctionTool:
    async def execute(
        arguments: JSONObject, context: ToolExecutionContext
    ) -> ToolOutput:
        return ToolOutput(name)

    return FunctionTool(ToolDefinition(name, name, {"type": "object"}), execute)


class SessionSpecValueTests(unittest.TestCase):
    def test_metadata_is_validated_and_nested_values_are_detached(self) -> None:
        original: dict[str, JSONValue] = {"nested": {"tags": ["one"]}}
        request = SessionOpenRequest(metadata=original)
        spec = SessionSpec(AgentComponents(FakeProvider([])), original)
        envelope = AgentEventEnvelope(metadata=original)
        original["nested"] = "changed"
        for snapshot in (request, spec, envelope):
            metadata = dict(snapshot.metadata)
            metadata["nested"]["tags"].append("two")
            self.assertEqual(dict(snapshot.metadata), {"nested": {"tags": ["one"]}})
            with self.assertRaises(TypeError):
                snapshot.metadata["extra"] = "invalid"
        cyclic: list[JSONValue] = []
        cyclic.append(cyclic)
        for invalid in (
            {"object": object()},
            {"number": float("nan")},
            {"number": float("inf")},
            {"tuple": ("value",)},
            {"nested": {1: "invalid"}},
            {"cycle": cyclic},
        ):
            with self.subTest(invalid=type(next(iter(invalid.values()))).__name__):
                with self.assertRaises(ValueError):
                    SessionOpenRequest(metadata=cast(JSONObject, invalid))
        for kwargs in ({"tenant_id": ""}, {"user_id": 4}, {"profile": "  "}):
            with self.assertRaises(ValueError):
                SessionOpenRequest(**kwargs)

    def test_redaction_and_serialization_include_envelope_metadata(self) -> None:
        events: list[AgentEvent] = []
        original = AgentEvent(
            "run_started",
            "live",
            envelope=AgentEventEnvelope(
                metadata={"tenant": "public", "nested": {"access_token": "test-only"}}
            ),
        )
        RedactingEventSink(CallbackEventSink(events.append)).emit(original)
        record = json.loads(serialize_agent_event(events[0]))
        self.assertEqual(
            record["metadata"],
            {
                "tenant": "public",
                "nested": {"access_token": "<redacted>"},
            },
        )
        self.assertEqual(
            dict(original.envelope.metadata)["nested"], {"access_token": "test-only"}
        )


class SessionSpecTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_selects_tools_and_prompts_with_full_observation_isolation(
        self,
    ) -> None:
        provider = FakeProvider(
            [
                [ToolCall("a", "alpha", {}), Finish("tool_calls")],
                [TextDelta("A"), Finish()],
                [ToolCall("b", "beta", {}), Finish("tool_calls")],
                [TextDelta("B"), Finish()],
            ]
        )
        events: list[AgentEvent] = []
        telemetry: list[TelemetryEvent] = []
        store = InMemorySessionStore()
        requests: list[SessionOpenRequest] = []

        class Telemetry:
            def on_event(self, event: TelemetryEvent) -> None:
                telemetry.append(event)

            async def aclose(self) -> None:
                pass

        class Resolver:
            def __init__(self, base: AgentComponents) -> None:
                self.specs = {
                    name: SessionSpec(
                        base.replace(
                            tools=(_tool(name),), system_prompt=f"Use {name}."
                        ),
                        {"tenant": f"metadata-{name}", "labels": {"region": ["local"]}},
                    )
                    for name in ("alpha", "beta")
                }

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                requests.append(request)
                return self.specs[request.profile]

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_store(store)
            .with_event_sink(CallbackEventSink(events.append))
            .with_telemetry_sink(Telemetry())
            .with_session_spec_resolver_factory(Resolver)
            .build()
        )
        async with runtime:
            for name in ("alpha", "beta"):
                async with await runtime.open_session(
                    name,
                    request=SessionOpenRequest(
                        profile=name, metadata={"untrusted": "hint"}
                    ),
                ) as session:
                    handle = await session.submit("question")
                    await handle.result()
                    observed = [event async for event in handle.events()]
                    self.assertTrue(observed)
                    self.assertTrue(
                        all(
                            event.envelope.metadata == session.metadata
                            for event in observed
                        )
                    )
                    self.assertEqual(
                        dict(session.metadata)["tenant"], f"metadata-{name}"
                    )
                session_events = [
                    event for event in events if event.envelope.session_id == name
                ]
                self.assertTrue(
                    all(
                        event.envelope.metadata["tenant"] == f"metadata-{name}"
                        for event in session_events
                    )
                )
                self.assertNotIn("metadata-", repr(await store.read_events(name)))
        self.assertEqual(
            [tools[0].name for tools in provider.requested_tools],
            ["alpha", "alpha", "beta", "beta"],
        )
        self.assertEqual(provider.requests[0][0].content, "Use alpha.")
        self.assertEqual(provider.requests[2][0].content, "Use beta.")
        self.assertNotIn("metadata-", repr(provider.requests))
        self.assertNotIn("metadata-", repr(provider.request_contexts))
        self.assertTrue(all(not event.envelope.metadata for event in telemetry))
        self.assertEqual([request.profile for request in requests], ["alpha", "beta"])

    async def test_every_open_resolves_and_conflicting_specs_are_rejected(self) -> None:
        provider = FakeProvider([[TextDelta("ok"), Finish()]])

        class Resolver:
            def __init__(self, base: AgentComponents) -> None:
                self.base = base
                self.calls = 0

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                self.calls += 1
                return SessionSpec(
                    self.base.replace(system_prompt=request.profile), request.metadata
                )

        resolvers: list[Resolver] = []

        def factory(base: AgentComponents) -> Resolver:
            resolvers.append(Resolver(base))
            return resolvers[-1]

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_pool(
                SessionPoolPolicy(max_active_sessions=1, idle_ttl_seconds=0)
            )
            .with_session_spec_resolver_factory(factory)
            .build()
        )
        request = SessionOpenRequest(profile="stable", metadata={"tenant": "one"})
        async with runtime:
            async with await runtime.open_session("s", request=request) as first:
                async with await runtime.open_session("s", request=request) as second:
                    self.assertEqual(second.metadata, first.metadata)
                for conflict in (
                    SessionOpenRequest(profile="changed", metadata={"tenant": "one"}),
                    SessionOpenRequest(profile="stable", metadata={"tenant": "other"}),
                ):
                    with self.assertRaises(RuntimeStateError):
                        await runtime.open_session("s", request=conflict)
                self.assertEqual(runtime.active_session_count, 1)
                self.assertEqual((await first.run("question")).message.content, "ok")
            # Even an expired controller rejects a change until actually evicted.
            with self.assertRaises(RuntimeStateError):
                await runtime.open_session(
                    "s", request=SessionOpenRequest(profile="changed")
                )
        self.assertEqual(resolvers[0].calls, 5)

    async def test_default_static_resolver_and_convenience_run(self) -> None:
        provider = FakeProvider([[Finish()], [Finish()]])
        components = AgentComponents(provider)
        resolver = StaticSessionSpecResolver(components)
        self.assertIs(
            (await resolver.resolve("s", SessionOpenRequest())).components, components
        )
        runtime = await AgentRuntimeBuilder().with_provider(provider).build()
        async with runtime:
            await runtime.run(
                "s", "first", request=SessionOpenRequest(metadata={"hint": "untrusted"})
            )
            async with await runtime.open_session("s") as session:
                self.assertEqual(dict(session.metadata), {})
                await session.run("second")

    async def test_all_component_changes_are_detected_by_identity_or_value(
        self,
    ) -> None:
        from pi_agent_py.api import Middleware, TruncateToolResults
        provider = FakeProvider([])
        base = AgentComponents(provider)
        changes = {
            "schema_validator": object(),
            "middleware": (Middleware("changed", tool_result=TruncateToolResults(64)),),
            "provider": FakeProvider([]),
            "tools": (_tool("new"),),
            "approval_policy": object(),
            "execution_gate": object(),
            "context_strategy": object(),
            "compaction": object(),
            "next_turn_hook": object(),
            "system_prompt": "changed",
            "max_model_requests": 2,
        }
        self.assertEqual(
            set(changes), {field.name for field in fields(AgentComponents)}
        )

        class Resolver:
            spec = SessionSpec(base)

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                return self.spec

        resolver = Resolver()
        runtime = (
            await AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_spec_resolver(resolver)
            .build()
        )
        async with runtime, await runtime.open_session("s"):
            for name, value in changes.items():
                with self.subTest(component=name):
                    resolver.spec = SessionSpec(base.replace(**{name: value}))
                    with self.assertRaises(RuntimeStateError):
                        await runtime.open_session("s")

    async def test_slow_resolution_does_not_lock_other_sessions_and_cancels_cleanly(
        self,
    ) -> None:
        started = asyncio.Event()
        unblock = asyncio.Event()

        class Resolver:
            def __init__(self, base: AgentComponents) -> None:
                self.spec = SessionSpec(base)

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                if session_id == "slow":
                    started.set()
                    await unblock.wait()
                return self.spec

        runtime = (
            await AgentRuntimeBuilder()
            .with_provider(FakeProvider([]))
            .with_session_spec_resolver_factory(Resolver)
            .build()
        )
        async with runtime:
            pending = asyncio.create_task(runtime.open_session("slow"))
            await started.wait()
            async with await runtime.open_session("fast"):
                self.assertEqual(runtime.active_session_count, 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertEqual(runtime.active_session_count, 1)
            unblock.set()
            async with await runtime.open_session("slow"):
                self.assertEqual(runtime.active_session_count, 2)

    async def test_resolution_failure_and_invalid_result_do_not_admit_controller(
        self,
    ) -> None:
        class Resolver:
            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                if session_id == "fail":
                    raise ValueError("private diagnostic")
                return cast(SessionSpec, None)

        runtime = (
            await AgentRuntimeBuilder()
            .with_provider(FakeProvider([]))
            .with_session_spec_resolver(Resolver())
            .build()
        )
        async with runtime:
            with self.assertRaises(ConfigurationError) as caught:
                await runtime.open_session("fail")
            self.assertIsInstance(caught.exception.__cause__, ValueError)
            self.assertNotIn("private", str(caught.exception))
            with self.assertRaises(ConfigurationError):
                await runtime.open_session("invalid")
            self.assertEqual(runtime.active_session_count, 0)

    async def test_shutdown_cancels_resolution_before_closing_owned_resolver(
        self,
    ) -> None:
        started = asyncio.Event()
        trace: list[str] = []

        class Resolver:
            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    trace.append("cancelled")
                raise AssertionError("unreachable")

            async def aclose(self) -> None:
                trace.append("closed")

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(FakeProvider([]))
            .with_session_spec_resolver(Resolver(), ownership="owned")
            .build()
        )
        pending = asyncio.create_task(runtime.open_session("pending"))
        await started.wait()
        await runtime.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(trace, ["cancelled", "closed"])
        self.assertEqual(runtime.active_session_count, 0)

    async def test_concurrent_open_conflict_admits_only_one_spec(self) -> None:
        arrived = 0
        both = asyncio.Event()

        class Resolver:
            def __init__(self, base: AgentComponents) -> None:
                self.base = base

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                nonlocal arrived
                arrived += 1
                if arrived == 2:
                    both.set()
                await both.wait()
                return SessionSpec(self.base, {"tenant": request.tenant_id})

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(FakeProvider([]))
            .with_session_spec_resolver_factory(Resolver)
            .build()
        )
        async with runtime:
            results = await asyncio.gather(
                *(
                    runtime.open_session(
                        "same", request=SessionOpenRequest(tenant_id=tenant)
                    )
                    for tenant in ("one", "two")
                ),
                return_exceptions=True,
            )
            self.assertEqual(
                sum(isinstance(result, RuntimeStateError) for result in results), 1
            )
            for result in results:
                if not isinstance(result, BaseException):
                    await result.close()
            self.assertEqual(runtime.active_session_count, 1)

    async def test_alternate_provider_and_cancelled_events_use_selected_metadata(
        self,
    ) -> None:
        base = FakeProvider([])
        selected = FakeProvider(
            [[TokenUsage(10, 2, 12), Finish()], [WaitUntilCancelled()]]
        )

        class Resolver:
            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                return SessionSpec(components, {"tenant": "selected"})

        components = AgentComponents(selected)
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(base, model="base")
            .with_pricing(PricingTable({"base": ModelPrice(Decimal(1), Decimal(1))}))
            .with_session_spec_resolver(Resolver())
            .build()
        )
        async with runtime, await runtime.open_session("s") as session:
            await session.run("first")
            self.assertEqual(base.requests, [])
            self.assertEqual(session.usage.usage, TokenUsage(10, 2, 12))
            self.assertIsNone(session.usage.estimated_cost_usd)
            self.assertEqual(session.usage.unpriced_requests, 1)
            active = await session.submit("wait")
            await selected.blocked.wait()
            queued = await session.follow_up("queued")
            await queued.abort()
            await active.abort()
            for handle in (active, queued):
                events = [event async for event in handle.events()]
                self.assertTrue(
                    all(event.envelope.metadata == session.metadata for event in events)
                )
                with self.assertRaises(asyncio.CancelledError):
                    await handle.result()

    async def test_builder_rolls_back_invalid_owned_factory_product(self) -> None:
        trace: list[str] = []

        class Invalid:
            async def aclose(self) -> None:
                trace.append("resolver")

        with self.assertRaises(RuntimeBuildError):
            await (
                AgentRuntimeBuilder()
                .with_provider(FakeProvider([]))
                .with_session_spec_resolver_factory(
                    lambda base: cast(SessionSpecResolver, Invalid())
                )
                .build()
            )
        self.assertEqual(trace, ["resolver"])

    async def test_concurrent_dispatchers_keep_provider_attempt_metadata_scoped(
        self,
    ) -> None:
        provider = FakeProvider([[WaitUntilCancelled()], [WaitUntilCancelled()]])
        started: asyncio.Queue[AgentEvent] = asyncio.Queue()

        def observe(event: AgentEvent) -> None:
            if event.kind == "provider_attempt_started":
                started.put_nowait(event)

        class Resolver:
            def __init__(self, base: AgentComponents) -> None:
                self.base = base

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                return SessionSpec(self.base, {"tenant": session_id})

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_spec_resolver_factory(Resolver)
            .with_event_sink(CallbackEventSink(observe))
            .build()
        )
        async with (
            runtime,
            await runtime.open_session("one") as one,
            await runtime.open_session("two") as two,
        ):
            first = await one.submit("wait")
            event_one = await started.get()
            second = await two.submit("also wait")
            event_two = await started.get()
            self.assertEqual(dict(event_one.envelope.metadata), {"tenant": "one"})
            self.assertEqual(dict(event_two.envelope.metadata), {"tenant": "two"})
            await first.abort()
            await second.abort()
            for handle, tenant in ((first, "one"), (second, "two")):
                self.assertTrue(
                    all(
                        [
                            event.envelope.metadata == {"tenant": tenant}
                            async for event in handle.events()
                        ]
                    )
                )

    async def test_eviction_and_reopen_keep_history_and_original_event_metadata(
        self,
    ) -> None:
        provider = FakeProvider(
            [[TextDelta("first"), Finish()], [TextDelta("again"), Finish()]]
        )
        events: list[AgentEvent] = []

        class Resolver:
            def __init__(self, base: AgentComponents) -> None:
                self.base = base
                self.closed = False

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                return SessionSpec(self.base, {"tenant": session_id})

            async def aclose(self) -> None:
                self.closed = True

        resolver = Resolver(AgentComponents(provider))
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_spec_resolver(resolver)
            .with_session_pool(
                SessionPoolPolicy(max_active_sessions=1, idle_ttl_seconds=None)
            )
            .with_event_sink(CallbackEventSink(events.append))
            .build()
        )
        async with runtime:
            async with await runtime.open_session("one") as first:
                await first.run("hello")
                await first.wait_until_idle()
            async with await runtime.open_session("two"):
                pass
            async with await runtime.open_session("one") as reopened:
                await reopened.run("continue")
                self.assertEqual(len((await reopened.snapshot()).messages), 4)
        evicted = [event for event in events if event.kind == "runtime_session_evicted"]
        self.assertEqual(
            [dict(event.envelope.metadata) for event in evicted],
            [{"tenant": "one"}, {"tenant": "two"}],
        )
        self.assertFalse(resolver.closed)

    async def test_restored_follow_up_and_session_plugin_events_use_resolved_metadata(
        self,
    ) -> None:
        store = InMemorySessionStore()
        await store.record_control(
            "restored",
            0,
            "control_enqueued",
            QueuedControl("pending", "follow_up", "resume", "existing-run"),
        )
        provider = FakeProvider([[TextDelta("restored"), Finish()]])
        events: list[AgentEvent] = []

        class Plugin:
            async def before_provider_request(
                self, context: ProviderRequestContext, plan: ContextPlan
            ) -> ContextPlan:
                return plan

            async def on_run_settled(self, run_id: str) -> None:
                pass

            async def close(self) -> None:
                pass

        def plugin(api: PluginAPI) -> None:
            api.register_session_plugin(lambda context: Plugin())

        class Resolver:
            def __init__(self, base: AgentComponents) -> None:
                self.spec = SessionSpec(base, {"tenant": "restored"})

            async def resolve(
                self, session_id: str, request: SessionOpenRequest
            ) -> SessionSpec:
                return self.spec

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_store(store)
            .with_plugin("session-observer", plugin)
            .with_event_sink(CallbackEventSink(events.append))
            .with_session_spec_resolver_factory(Resolver)
            .build()
        )
        async with runtime, await runtime.open_session("restored") as session:
            await session.wait_until_idle()
            self.assertEqual(
                (await session.snapshot()).messages[-1].content, "restored"
            )
        observed = [
            event for event in events if event.envelope.session_id == "restored"
        ]
        self.assertTrue(
            all(event.envelope.metadata == {"tenant": "restored"} for event in observed)
        )
        self.assertIn("plugin_session_closed", {event.kind for event in observed})
        self.assertIn("provider_attempt_started", {event.kind for event in observed})
        self.assertNotIn("tenant", repr(await store.read_events("restored")))
