from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from pi_agent_py.api import (
    Agent,
    AgentComponents,
    AgentProfile,
    AgentRuntimeBuilder,
    AgentSession,
    CompactionComponents,
    Finish,
    InMemoryEventSink,
    InMemorySessionStore,
    Message,
    Middleware,
    MiddlewareError,
    RedactingEventSink,
    RuntimeBuildError,
    RuntimeStateError,
    SessionSpec,
    ToolCall,
    ToolOutput,
    TruncateToolResults,
    tool,
)
from pi_agent_py.errors import PluginLoadError, SandboxError, SandboxTimeoutError
from pi_agent_py.plugins import PluginHost
from pi_agent_py.testing import FakeCompactionStrategy, FakeProvider
from downstream_middleware.rag import rag_middleware


@tool
async def echo() -> str:
    """Return a long result."""
    return "original-sensitive-result" * 15


def tool_provider(name="echo", arguments=None, finish="tool_calls"):
    return FakeProvider(
        [[ToolCall("call", name, arguments or {}), Finish(finish)], [Finish()]]
    )


class Request:
    def __init__(self, action=None):
        self.action = action
        self.calls = []

    async def prepare(self, context, plan):
        self.calls.append(context)
        return self.action(plan) if self.action else plan


class Result:
    def __init__(self, action=None):
        self.action = action
        self.calls = []

    async def transform(self, context, output):
        self.calls.append(context)
        return self.action(output) if self.action else output


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def test_construction_and_component_snapshot(self):
        for options in (
            {"name": ""},
            {"name": "none"},
            {"name": "p", "provider_request": Request(), "priority": True},
            *(
                {"name": "t", "provider_request": Request(), "timeout_seconds": t}
                for t in (-1, float("inf"), float("nan"), True)
            ),
        ):
            with (
                self.subTest(options=options),
                self.assertRaises((ValueError, TypeError)),
            ):
                Middleware(**options)
        middleware = rag_middleware()
        items = [middleware]
        components = AgentComponents(FakeProvider([]), middleware=items)
        items.clear()
        self.assertEqual(components.middleware, (middleware,))
        with self.assertRaisesRegex(ValueError, "duplicate middleware"):
            components.replace(middleware=(middleware, middleware))

    async def test_request_projection_is_not_durable_in_standalone_or_session(self):
        for session_mode in (False, True):
            provider, store = tool_provider(), InMemorySessionStore()
            components = AgentComponents(
                provider, tools=(echo,), middleware=(rag_middleware(),)
            )
            agent = (
                AgentSession("s", store, components)
                if session_mode
                else Agent(components)
            )
            result = await agent.run("question")
            self.assertEqual(len(provider.requests), 2)
            for request in provider.requests:
                self.assertEqual(request[0].content, "retrieved-reference")
            self.assertNotIn("retrieved-reference", repr(result.messages))
            self.assertNotIn("retrieved-reference", repr(await store.read_events("s")))
            if session_mode:
                await agent.close()

    async def test_request_order_and_each_turn(self):
        observed = []

        def append(label):
            def apply(plan):
                observed.append(
                    (label, tuple(message.content for message in plan.messages))
                )
                return replace(
                    plan, messages=plan.messages + (Message("system", label),)
                )

            return Request(apply)

        first, second, last = append("a"), append("b"), append("c")
        provider, events = tool_provider(), InMemoryEventSink()
        middleware = (
            Middleware("c", provider_request=last, priority=1),
            Middleware("a", provider_request=first, priority=-1),
            Middleware("b", provider_request=second, priority=-1),
        )
        await Agent(
            AgentComponents(provider, tools=(echo,), middleware=middleware),
            event_sink=events,
        ).run("q")
        self.assertEqual([name for name, _ in observed], ["a", "b", "c"] * 2)
        self.assertEqual(observed[1][1][-1], "a")
        self.assertEqual([c.request_number for c in first.calls], [1, 2])
        prepared = [
            event for event in events.events if event.kind == "context_prepared"
        ]
        self.assertEqual(
            prepared[-1].payload["selected_message_count"], len(provider.requests[-1])
        )

    async def test_request_deepcopy_protects_nested_history(self):
        def mutate(plan):
            for message in plan.messages:
                if message.role == "tool":
                    message.structured_content["nested"].append("changed")
            return plan

        @tool
        async def data():
            """Return JSON."""
            return {"nested": ["original"]}

        provider, store = tool_provider("data"), InMemorySessionStore()
        async with AgentSession(
            "s",
            store,
            AgentComponents(
                provider,
                tools=(data,),
                middleware=(Middleware("mutate", provider_request=Request(mutate)),),
            ),
        ) as session:
            result = await session.run("q")
        self.assertEqual(
            result.messages[2].structured_content, {"nested": ["original"]}
        )
        self.assertEqual(
            (await store.load("s")).messages[2].structured_content,
            {"nested": ["original"]},
        )

    async def test_request_failure_is_classified_and_safe(self):
        def crash(plan):
            raise RuntimeError("private-secret")

        for action in (
            lambda p: None,
            lambda p: replace(p, source_message_count=99),
            crash,
        ):
            provider, events = FakeProvider([[Finish()]]), InMemoryEventSink()
            with (
                self.subTest(action=action),
                self.assertRaises(MiddlewareError) as caught,
            ):
                await Agent(
                    AgentComponents(
                        provider,
                        middleware=(
                            Middleware("bad", provider_request=Request(action)),
                        ),
                    ),
                    event_sink=events,
                ).run("q")
            self.assertIsNotNone(caught.exception.__cause__)
            self.assertNotIn("private-secret", str(caught.exception))
            self.assertEqual(provider.requests, [])
            failure = next(e for e in events.events if e.kind == "run_failed")
            self.assertEqual(failure.payload["error_category"], "middleware")
            self.assertNotIn("private-secret", repr(events.events))

    async def test_request_timeout_and_cancellation(self):
        for timeout in (0, None):
            started, events = asyncio.Event(), InMemoryEventSink()

            class Wait:
                async def prepare(self, context, plan):
                    started.set()
                    await asyncio.Event().wait()

            middleware = Middleware(
                "wait", provider_request=Wait(), timeout_seconds=timeout
            )
            task = asyncio.create_task(
                Agent(
                    AgentComponents(FakeProvider([]), middleware=(middleware,)),
                    event_sink=events,
                ).run("q")
            )
            await started.wait()
            if timeout is None:
                task.cancel()
            with self.assertRaises(
                asyncio.CancelledError if timeout is None else MiddlewareError
            ) as caught:
                await task
            if timeout == 0:
                self.assertIsInstance(caught.exception.__cause__, TimeoutError)
            self.assertIn(
                "middleware_cancelled" if timeout is None else "middleware_failed",
                [e.kind for e in events.events],
            )

    async def test_session_plugin_runs_after_components_and_compaction_bypasses_chain(
        self,
    ):
        seen, request = [], Request()

        class Plugin:
            async def before_provider_request(self, context, plan):
                seen.append(plan.messages[0].content)
                return plan

            async def on_run_settled(self, run_id):
                pass

            async def close(self):
                pass

        host = PluginHost()
        await host.load(
            "session",
            lambda api: (api.register_session_plugin(lambda c: Plugin()), None)[1],
        )
        provider, store = FakeProvider([[Finish()], [Finish()]]), InMemorySessionStore()
        components = AgentComponents(
            provider,
            middleware=(
                rag_middleware(),
                Middleware("record", provider_request=request),
            ),
            compaction=CompactionComponents(FakeCompactionStrategy(["summary"])),
        )
        async with AgentSession("s", store, components, plugin_host=host) as session:
            await session.run("one")
            await session.run("two")
            self.assertEqual(seen, ["retrieved-reference"] * 2)
            await session.compact()
            self.assertEqual(len(request.calls), 2)
        await host.close()

    async def test_truncation_is_durable_and_visible_next_turn_and_fork(self):
        provider, store, events = (
            tool_provider(),
            InMemorySessionStore(),
            InMemoryEventSink(),
        )
        recorder = Result()
        components = AgentComponents(
            provider,
            tools=(echo,),
            middleware=(
                Middleware("truncate", tool_result=TruncateToolResults(64)),
                Middleware("record", tool_result=recorder),
            ),
        )
        async with AgentSession("s", store, components, event_sink=events) as session:
            result = await session.run("q")
        text = result.messages[2].content
        self.assertEqual(len(text), 64)
        self.assertEqual(provider.requests[1][2].content, text)
        self.assertEqual((await store.fork("s", "fork")).messages[2].content, text)
        self.assertEqual(recorder.calls[0].outcome, "success")
        self.assertEqual(recorder.calls[0].execution.call.id, "call")
        self.assertIsNotNone(recorder.calls[0].run_id)
        summary = next(e for e in events.events if e.kind == "tool_result_transformed")
        self.assertTrue(summary.payload["changed"])
        self.assertEqual(summary.payload["middleware_names"], ["truncate", "record"])
        self.assertEqual(summary.payload["final_content_chars"], 64)

    async def test_result_order_and_mutation_are_isolated(self):
        outputs, calls = [], []

        @tool
        async def data():
            """Return a shared output for isolation testing."""
            value = ToolOutput("original", {"values": []})
            outputs.append(value)
            return value

        def mutate(output):
            output.structured_content["values"].append("first")
            return replace(output, content="transformed")

        def second(output):
            calls.append((output.content, output.structured_content))
            return output

        middleware = (
            Middleware("second", tool_result=Result(second), priority=2),
            Middleware("first", tool_result=Result(mutate), priority=1),
        )
        result = await Agent(
            AgentComponents(tool_provider("data"), tools=(data,), middleware=middleware)
        ).run("q")
        self.assertEqual(outputs[0].structured_content, {"values": []})
        self.assertEqual(calls, [("transformed", {"values": ["first"]})])
        self.assertEqual(result.messages[2].structured_content, {"values": ["first"]})

    async def test_failed_tool_cannot_be_transformed_to_success(self):
        @tool
        async def failure():
            """Return an error."""
            return ToolOutput("original-sensitive-result", is_error=True)

        components = AgentComponents(
            tool_provider("failure"),
            tools=(failure,),
            middleware=(
                Middleware(
                    "flip", tool_result=Result(lambda o: replace(o, is_error=False))
                ),
            ),
        )
        result = await Agent(components).run("q")
        self.assertTrue(result.messages[2].is_error)
        self.assertEqual(result.messages[2].structured_content["tool_outcome"], "error")

    async def test_result_failures_commit_only_safe_unknown_result(self):
        def crash(output):
            raise RuntimeError("private-secret")

        class Wait:
            async def transform(self, context, output):
                await asyncio.Event().wait()

        for hook, timeout in (
            (Result(crash), None),
            (Result(lambda o: replace(o, is_error=True)), None),
            (Result(lambda o: None), None),
            (Wait(), 0),
        ):
            store, provider, later = InMemorySessionStore(), tool_provider(), Result()
            components = AgentComponents(
                provider,
                tools=(echo,),
                middleware=(
                    Middleware("fail", tool_result=hook, timeout_seconds=timeout),
                    Middleware("later", tool_result=later),
                ),
            )
            async with AgentSession("s", store, components) as session:
                result = await session.run("q")
            self.assertTrue(result.messages[2].is_error)
            self.assertIn("do not retry automatically", result.messages[2].content)
            self.assertEqual(
                result.messages[2].structured_content,
                {
                    "status": "middleware_failed",
                    "middleware": "fail",
                    "tool_outcome": "success",
                },
            )
            self.assertNotIn(
                "original-sensitive-result", repr(await store.read_events("s"))
            )
            self.assertNotIn("private-secret", repr(await store.read_events("s")))
            self.assertEqual(len(provider.requests), 2)
            self.assertEqual(later.calls, [])

    async def test_result_cancellation_leaves_pending_call_for_recovery(self):
        started = asyncio.Event()

        class Wait:
            async def transform(self, context, output):
                started.set()
                await asyncio.Event().wait()

        store, provider, events = (
            InMemorySessionStore(),
            tool_provider(),
            InMemoryEventSink(),
        )
        session = AgentSession(
            "s",
            store,
            AgentComponents(
                provider,
                tools=(echo,),
                middleware=(Middleware("wait", tool_result=Wait()),),
            ),
            event_sink=events,
        )
        task = asyncio.create_task(session.run("q"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(
            any(m.role == "tool" for m in (await store.load("s")).messages)
        )
        await session.close()
        async with AgentSession(
            "s", store, AgentComponents(FakeProvider([[Finish()]]))
        ) as reopened:
            await reopened.run("continue")
        recovered = [m for m in (await store.load("s")).messages if m.role == "tool"]
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].is_error)
        self.assertIn("middleware_cancelled", [e.kind for e in events.events])

    async def test_outcomes_and_runtime_generated_results_scope(self):
        async def wait():
            """Wait for timeout."""
            await asyncio.Event().wait()

        @tool
        async def error():
            """Return an error."""
            return ToolOutput("error", is_error=True)

        @tool
        async def sandbox_timeout():
            """Fake sandbox timeout."""
            raise SandboxTimeoutError("private")

        @tool
        async def sandbox_error():
            """Fake sandbox failure."""
            raise SandboxError("private")

        cases = [
            (echo, "success"),
            (error, "error"),
            (tool(wait, timeout_seconds=0), "timeout"),
            (tool(echo.fn, side_effects="write"), "execution_denied"),
            (sandbox_timeout, "sandbox_timeout"),
            (sandbox_error, "sandbox_error"),
        ]
        for adapted, outcome in cases:
            hook = Result()
            await Agent(
                AgentComponents(
                    tool_provider(adapted.definition.name),
                    tools=(adapted,),
                    middleware=(Middleware("record", tool_result=hook),),
                )
            ).run("q")
            self.assertEqual([c.outcome for c in hook.calls], [outcome])
        for name, arguments, finish, adapted in (
            ("missing", {}, "tool_calls", echo),
            ("echo", {"extra": 1}, "tool_calls", echo),
            ("echo", {}, "tool_calls", tool(echo.fn, requires_approval=True)),
            ("echo", {}, "length", echo),
        ):
            hook = Result()
            await Agent(
                AgentComponents(
                    tool_provider(name, arguments, finish),
                    tools=(adapted,),
                    middleware=(Middleware("record", tool_result=hook),),
                )
            ).run("q")
            self.assertEqual(hook.calls, [])

    async def test_events_pair_and_redaction_and_unchanged_result(self):
        events, redacted = InMemoryEventSink(), InMemoryEventSink()

        class Tee:
            def emit(self, event):
                events.emit(event)
                RedactingEventSink(redacted).emit(event)

        await Agent(
            AgentComponents(
                tool_provider(),
                tools=(echo,),
                middleware=(
                    Middleware(
                        "both", provider_request=Request(), tool_result=Result()
                    ),
                ),
            ),
            event_sink=Tee(),
        ).run("q")
        starts = [e for e in events.events if e.kind == "middleware_started"]
        ends = [e for e in events.events if e.kind == "middleware_finished"]
        self.assertEqual(len(starts), 3)
        self.assertEqual(
            [e.envelope.span_id for e in starts], [e.envelope.span_id for e in ends]
        )
        for event in ends:
            self.assertGreaterEqual(event.envelope.duration_ms, 0)
        summary = next(e for e in events.events if e.kind == "tool_result_transformed")
        self.assertFalse(summary.payload["changed"])
        self.assertEqual(
            summary.payload["original_content_chars"],
            summary.payload["final_content_chars"],
        )
        observed = [
            e
            for e in redacted.events
            if e.kind.startswith("middleware_") or e.kind == "tool_result_transformed"
        ]
        self.assertNotIn("original-sensitive-result", repr(observed))
        self.assertTrue(
            all(
                not {"content", "text", "output", "message"} & e.payload.keys()
                for e in observed
            )
        )

    async def test_profile_builder_and_downstream_plugin_assembly(self):
        calls = []

        def factory():
            calls.append("factory")
            return rag_middleware()

        profile = AgentProfile("profile", middleware_factories=(factory,))
        profile.build_components(FakeProvider([]))
        self.assertEqual(calls, ["factory"])
        for plugin in (False, True):
            provider = FakeProvider([[Finish()]])
            builder = (
                AgentRuntimeBuilder()
                .with_provider(provider)
                .with_profile(profile)
                .with_middleware(())
            )
            if plugin:

                def register(api):
                    api.register_middleware(rag_middleware())

                builder.with_plugin("rag", register)
            else:
                builder.with_middleware((rag_middleware(),))
            async with await builder.build() as runtime:
                await runtime.run("s", "q")
            self.assertEqual(provider.requests[0][0].content, "retrieved-reference")
        self.assertEqual(calls, ["factory"])

    async def test_plugin_atomic_conflict_unregister_and_cleanup(self):
        host, handles, cleanup = PluginHost(), [], []

        def first(api):
            handles.append(api.register_middleware(rag_middleware()))

        await host.load("first", first)

        def conflicting(api):
            api.register_cleanup(lambda: cleanup.append("rolled back"))
            api.register_tool(echo)
            api.register_middleware(rag_middleware())

        with self.assertRaises(PluginLoadError):
            await host.load("second", conflicting)
        self.assertEqual(cleanup, ["rolled back"])
        self.assertEqual(host.snapshot().tools, ())
        handles[0].unregister()
        self.assertEqual(host.snapshot().middleware, ())
        await host.load("replacement", conflicting)
        await host.unload("replacement")
        self.assertEqual(host.snapshot().middleware, ())
        await host.close()
        with self.assertRaises(RuntimeBuildError):
            await (
                AgentRuntimeBuilder()
                .with_provider(FakeProvider([]))
                .with_middleware((rag_middleware(),))
                .with_plugin(
                    "duplicate",
                    lambda api: (api.register_middleware(rag_middleware()), None)[1],
                )
                .build()
            )

    async def test_plugin_appended_after_builder_and_spec_identity(self):
        seen = []

        def mark(label):
            return Middleware(
                label, provider_request=Request(lambda p: (seen.append(label), p)[1])
            )

        first, second = mark("first"), mark("second")
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(FakeProvider([[Finish()]]))
            .with_middleware((first,))
            .with_plugin("p", lambda api: (api.register_middleware(second), None)[1])
            .build()
        )
        async with runtime:
            await runtime.run("s", "q")
        self.assertEqual(seen, ["first", "second"])
        base = AgentComponents(FakeProvider([]), middleware=(first,))

        class Resolver:
            spec = SessionSpec(base)

            async def resolve(self, session_id, request):
                return self.spec

        resolver = Resolver()
        runtime = (
            await AgentRuntimeBuilder()
            .with_provider(base.provider)
            .with_session_spec_resolver(resolver)
            .build()
        )
        async with runtime, await runtime.open_session("s"):
            resolver.spec = SessionSpec(base.replace(middleware=tuple([first])))
            async with await runtime.open_session("s"):
                pass
            resolver.spec = SessionSpec(base.replace(middleware=(mark("first"),)))
            with self.assertRaises(RuntimeStateError):
                await runtime.open_session("s")
