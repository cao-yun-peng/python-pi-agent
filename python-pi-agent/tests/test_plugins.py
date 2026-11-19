from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from pi_agent_py import (
    AgentComponents,
    Agent,
    AgentSession,
    ContextPlan,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    InMemorySessionStore,
    JSONObject,
    Message,
    NextTurnContext,
    PluginAPI,
    PluginExecutionError,
    PluginHost,
    PluginLoadError,
    PluginRegistrationError,
    ProviderRequestContext,
    SessionPluginContext,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    WaitUntilCancelled,
)


def _next_turn_context() -> NextTurnContext:
    return NextTurnContext(
        session_id="session-1",
        run_id="run-1",
        turn_id="run-1:turn:2",
        next_request_number=2,
    )


class RecordingHook:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        context: NextTurnContext,
    ) -> Sequence[Message]:
        self._calls.append(f"{self._name}:{context.next_request_number}")
        return messages


class RecordingSessionPlugin:
    def __init__(
        self,
        session_id: str,
        calls: list[str],
    ) -> None:
        self._session_id = session_id
        self._calls = calls

    async def before_provider_request(
        self,
        context: ProviderRequestContext,
        plan: ContextPlan,
    ) -> ContextPlan:
        self._calls.append(
            f"request:{self._session_id}:{context.session_id}:{context.request_number}"
        )
        return ContextPlan(
            messages=(
                Message(role="system", content=f"session:{self._session_id}"),
                *plan.messages,
            ),
            source_message_count=plan.source_message_count,
            omitted_message_count=plan.omitted_message_count,
            token_budget=plan.token_budget,
            estimated_tokens=plan.estimated_tokens,
        )

    async def on_run_settled(self, run_id: str) -> None:
        self._calls.append(f"settled:{self._session_id}:{run_id}")

    async def close(self) -> None:
        self._calls.append(f"closed:{self._session_id}")


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_plugins_are_isolated_and_only_change_request_projection(
        self,
    ) -> None:
        calls: list[str] = []
        opened: list[str] = []
        host = PluginHost()

        def plugin(api: PluginAPI) -> None:
            def open_session(context: SessionPluginContext) -> RecordingSessionPlugin:
                opened.append(context.session_id)
                return RecordingSessionPlugin(context.session_id, calls)

            api.register_session_plugin(open_session)

        await host.load("session-context", plugin)
        first_store = InMemorySessionStore()
        second_store = InMemorySessionStore()
        first_provider = FakeProvider([[TextDelta("one"), Finish()]])
        second_provider = FakeProvider([[TextDelta("two"), Finish()]])
        first = AgentSession("first", first_store, AgentComponents(first_provider), plugin_host=host)
        second = AgentSession(
            "second",
            second_store,
            AgentComponents(second_provider),
            plugin_host=host,
        )

        await first.run("hello")
        await second.run("world")
        await first.close()
        await second.close()

        self.assertEqual(opened, ["first", "second"])
        self.assertEqual(first_provider.requests[0][0].content, "session:first")
        self.assertEqual(second_provider.requests[0][0].content, "session:second")
        self.assertEqual(
            [message.content for message in (await first_store.load("first")).messages],
            ["hello", "one"],
        )
        self.assertIn("closed:first", calls)
        self.assertIn("closed:second", calls)
        self.assertTrue(any(call.startswith("settled:first:") for call in calls))

    async def test_unload_rejects_an_active_session_lease(self) -> None:
        host = PluginHost()

        def plugin(api: PluginAPI) -> None:
            api.register_session_plugin(
                lambda context: RecordingSessionPlugin(context.session_id, [])
            )

        await host.load("leased", plugin)
        lease = await host.open_session("demo")

        with self.assertRaisesRegex(
            PluginRegistrationError,
            "active session leases",
        ):
            await host.unload("leased")

        await lease.close()
        await host.unload("leased")

    async def test_session_request_hook_timeout_is_fail_closed(self) -> None:
        class BlockingSessionPlugin(RecordingSessionPlugin):
            async def before_provider_request(
                self,
                context: ProviderRequestContext,
                plan: ContextPlan,
            ) -> ContextPlan:
                del context, plan
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        host = PluginHost()

        def plugin(api: PluginAPI) -> None:
            api.register_session_plugin(
                lambda context: BlockingSessionPlugin(context.session_id, []),
                hook_timeout_seconds=0,
            )

        await host.load("blocking-session", plugin)
        provider = FakeProvider([[TextDelta("unused"), Finish()]])
        session = AgentSession(
            "demo",
            InMemorySessionStore(),
            AgentComponents(provider),
            plugin_host=host,
        )

        with self.assertRaisesRegex(
            PluginExecutionError,
            "provider request hook",
        ):
            await session.run("hello")

        self.assertEqual(provider.requests, [])
        await session.close()

    async def test_abort_then_close_releases_session_plugin(self) -> None:
        calls: list[str] = []
        host = PluginHost()

        def plugin(api: PluginAPI) -> None:
            api.register_session_plugin(
                lambda context: RecordingSessionPlugin(context.session_id, calls)
            )

        await host.load("lifecycle", plugin)
        provider = FakeProvider([[WaitUntilCancelled()]])
        session = AgentSession(
            "demo",
            InMemorySessionStore(),
            AgentComponents(provider),
            plugin_host=host,
        )
        task = asyncio.create_task(session.run("wait"))
        await provider.blocked.wait()

        await session.abort()
        await session.close()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls.count("closed:demo"), 1)

    async def test_host_close_releases_active_session_leases_first(self) -> None:
        calls: list[str] = []
        host = PluginHost()

        def plugin(api: PluginAPI) -> None:
            api.register_session_plugin(
                lambda context: RecordingSessionPlugin(context.session_id, calls)
            )

        await host.load("session-owned", plugin)
        await host.open_session("demo")

        await host.close()

        self.assertEqual(calls, ["closed:demo"])

    async def test_plugin_tools_and_hooks_form_an_explicit_runtime_snapshot(
        self,
    ) -> None:
        hook_calls: list[str] = []
        events = InMemoryEventSink()
        host = PluginHost(event_sink=events)

        def calculator(api: PluginAPI) -> None:
            api.register_tool(_add_tool())
            api.register_next_turn_hook(
                RecordingHook("calculator", hook_calls)
            )

        await host.load("calculator", calculator)
        capabilities = host.snapshot()
        provider = FakeProvider(
            [
                [ToolCall("call-1", "add", {"a": 2, "b": 3}), Finish("tool_calls")],
                [TextDelta("5"), Finish()],
            ]
        )
        agent = Agent(
            AgentComponents(
                provider,
                tools=capabilities.tools,
                next_turn_hook=capabilities.next_turn_hook,
            ),
            event_sink=events,
        )

        result = await agent.run("calculate")

        self.assertEqual(result.message.content, "5")
        self.assertEqual(capabilities.plugin_names, ("calculator",))
        self.assertEqual(hook_calls, ["calculator:2"])
        self.assertEqual(
            [event.kind for event in events.events if event.kind.startswith("plugin_")],
            ["plugin_loaded", "plugin_hook_started", "plugin_hook_finished"],
        )
        hook_events = [
            event
            for event in events.events
            if event.payload.get("hook") == "next_turn"
        ]
        self.assertEqual(
            hook_events[0].envelope.span_id,
            hook_events[1].envelope.span_id,
        )
        self.assertIsNotNone(hook_events[1].envelope.duration_ms)
        self.assertEqual(hook_events[1].envelope.source, "plugin_next_turn_hook")
        self.assertIsNotNone(hook_events[1].envelope.run_id)
        self.assertEqual(
            hook_events[1].envelope.turn_id,
            f"{hook_events[1].envelope.run_id}:turn:2",
        )

    async def test_hooks_run_by_priority_then_registration_order(self) -> None:
        calls: list[str] = []
        host = PluginHost()

        def first(api: PluginAPI) -> None:
            api.register_next_turn_hook(RecordingHook("first", calls), priority=10)
            api.register_next_turn_hook(RecordingHook("second", calls), priority=10)

        def early(api: PluginAPI) -> None:
            api.register_next_turn_hook(RecordingHook("early", calls), priority=-5)

        await host.load("first-plugin", first)
        await host.load("early-plugin", early)
        hook = host.snapshot().next_turn_hook
        assert hook is not None

        prepared = await hook.prepare(
            (Message(role="user", content="hello"),),
            context=_next_turn_context(),
        )

        self.assertEqual(tuple(prepared), (Message(role="user", content="hello"),))
        self.assertEqual(calls, ["early:2", "first:2", "second:2"])

    async def test_failed_load_rolls_back_registrations_and_resources(self) -> None:
        cleanup_calls: list[str] = []
        host = PluginHost()

        async def broken(api: PluginAPI) -> None:
            api.register_tool(_add_tool())
            api.register_cleanup(lambda: cleanup_calls.append("released"))
            raise RuntimeError("setup failed")

        with self.assertRaisesRegex(PluginLoadError, 'plugin "broken" failed'):
            await host.load("broken", broken)

        capabilities = host.snapshot()
        self.assertEqual(capabilities.plugin_names, ())
        self.assertEqual(capabilities.tools, ())
        self.assertIsNone(capabilities.next_turn_hook)
        self.assertEqual(cleanup_calls, ["released"])

    async def test_conflicting_tool_does_not_partially_publish_plugin(self) -> None:
        cleanup_calls: list[str] = []
        host = PluginHost()

        def owner(api: PluginAPI) -> None:
            api.register_tool(_add_tool())

        await host.load("owner", owner)

        def conflicting(api: PluginAPI) -> None:
            api.register_tool(_add_tool())
            api.register_next_turn_hook(RecordingHook("hidden", []))
            api.register_cleanup(lambda: cleanup_calls.append("rolled-back"))

        with self.assertRaises(PluginLoadError) as raised:
            await host.load("conflicting", conflicting)

        self.assertIsInstance(raised.exception.__cause__, Exception)
        capabilities = host.snapshot()
        self.assertEqual(capabilities.plugin_names, ("owner",))
        self.assertEqual(
            [tool.definition.name for tool in capabilities.tools],
            ["add"],
        )
        self.assertIsNone(capabilities.next_turn_hook)
        self.assertEqual(cleanup_calls, ["rolled-back"])

    async def test_registration_handle_and_unload_remove_capabilities(self) -> None:
        cleanup_calls: list[str] = []
        registrations = []
        host = PluginHost()

        def plugin(api: PluginAPI) -> None:
            registrations.append(api.register_tool(_add_tool()))
            api.register_cleanup(lambda: cleanup_calls.append("second"))
            api.register_cleanup(lambda: cleanup_calls.append("first"))

        handle = await host.load("temporary", plugin)
        self.assertEqual(len(host.snapshot().tools), 1)

        registrations[0].unregister()
        registrations[0].unregister()
        self.assertEqual(host.snapshot().tools, ())

        await handle.close()
        await handle.close()
        self.assertFalse(handle.active)
        self.assertEqual(cleanup_calls, ["first", "second"])

    async def test_hook_timeout_is_fail_closed_and_observable(self) -> None:
        class BlockingHook:
            async def prepare(
                self,
                messages: Sequence[Message],
                *,
                context: NextTurnContext,
            ) -> Sequence[Message]:
                del messages, context
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        events = InMemoryEventSink()
        host = PluginHost(event_sink=events)

        def blocking(api: PluginAPI) -> None:
            api.register_next_turn_hook(BlockingHook(), timeout_seconds=0)

        await host.load(
            "blocking",
            blocking,
        )
        hook = host.snapshot().next_turn_hook
        assert hook is not None

        with self.assertRaisesRegex(PluginExecutionError, 'plugin "blocking"'):
            await hook.prepare((), context=_next_turn_context())

        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["plugin_hook_started", "plugin_hook_failed"],
        )
        self.assertEqual(events.events[-1].payload["error"], "TimeoutError")

    async def test_cancelling_hook_propagates_and_stops_later_hooks(self) -> None:
        started = asyncio.Event()
        later_calls: list[str] = []

        class BlockingHook:
            async def prepare(
                self,
                messages: Sequence[Message],
                *,
                context: NextTurnContext,
            ) -> Sequence[Message]:
                del messages, context
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        events = InMemoryEventSink()
        host = PluginHost(event_sink=events)

        def plugin(api: PluginAPI) -> None:
            api.register_next_turn_hook(BlockingHook())
            api.register_next_turn_hook(RecordingHook("later", later_calls))

        await host.load("cancel", plugin)
        hook = host.snapshot().next_turn_hook
        assert hook is not None
        task = asyncio.create_task(hook.prepare((), context=_next_turn_context()))
        await started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(later_calls, [])
        self.assertEqual(events.events[-1].kind, "plugin_hook_cancelled")

    async def test_host_closes_plugins_in_reverse_load_order(self) -> None:
        cleanup_calls: list[str] = []
        host = PluginHost()

        def first(api: PluginAPI) -> None:
            api.register_cleanup(lambda: cleanup_calls.append("first"))

        def second(api: PluginAPI) -> None:
            api.register_cleanup(lambda: cleanup_calls.append("second"))

        await host.load("first", first)
        await host.load("second", second)

        await host.close()
        await host.close()

        self.assertEqual(cleanup_calls, ["second", "first"])
        self.assertEqual(host.snapshot().plugin_names, ())


def _add_tool() -> FunctionTool:
    async def add(
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del context
        left = arguments["a"]
        right = arguments["b"]
        assert isinstance(left, int) and not isinstance(left, bool)
        assert isinstance(right, int) and not isinstance(right, bool)
        return ToolOutput(content=str(left + right))

    return FunctionTool(
        ToolDefinition(
            name="add",
            description="Add two integers.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
            retry_safe=True,
            replay_safe=True,
        ),
        add,
    )


if __name__ == "__main__":
    unittest.main()
