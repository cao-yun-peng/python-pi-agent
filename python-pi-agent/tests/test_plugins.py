from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from pi_agent_py import (
    Agent,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    JSONObject,
    Message,
    PluginAPI,
    PluginExecutionError,
    PluginHost,
    PluginLoadError,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolOutput,
)


class RecordingHook:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        next_request_number: int,
    ) -> Sequence[Message]:
        self._calls.append(f"{self._name}:{next_request_number}")
        return messages


class PluginTests(unittest.IsolatedAsyncioTestCase):
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
            provider,
            tools=capabilities.tools,
            next_turn_hook=capabilities.next_turn_hook,
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
            next_request_number=2,
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
                next_request_number: int,
            ) -> Sequence[Message]:
                del messages, next_request_number
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
            await hook.prepare((), next_request_number=2)

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
                next_request_number: int,
            ) -> Sequence[Message]:
                del messages, next_request_number
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
        task = asyncio.create_task(hook.prepare((), next_request_number=2))
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
    async def add(arguments: JSONObject) -> ToolOutput:
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
