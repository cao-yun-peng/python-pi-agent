"""Tool contracts stay stable while implementations and their resources stay live."""

import asyncio
import unittest
from copy import deepcopy
from dataclasses import replace

from pi_agent_py.api import (
    Agent, AgentComponents, DeclaredSideEffectGate, Finish, StaticApprovalPolicy,
    ToolCall, ToolDefinition, ToolOutput,
)
from pi_agent_py.testing import FakeProvider


class MutableTool:
    def __init__(self, definition, handler):
        self.current = definition
        self.handler = handler
        self.reads = 0

    @property
    def definition(self):
        self.reads += 1
        return self.current

    async def execute(self, arguments, context):
        return await self.handler(arguments, context)

    def __deepcopy__(self, memo):
        raise AssertionError("live tool implementations must not be copied")


def definition(**options):
    return ToolDefinition("echo", "Echo integers", {
        "type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]}},
        "required": ["n"], "additionalProperties": False,
    }, **options)


class DefinitionValueTests(unittest.TestCase):
    def test_original_nested_and_exposed_values_cannot_mutate_definition(self):
        schema = {
            "type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]}},
            "required": ["n"], "default": {"items": [{"value": 1}]},
        }
        expected = deepcopy(schema)
        value = ToolDefinition("echo", "Echo", schema)
        schema["properties"].clear()
        schema["required"].clear()
        value.input_schema["properties"]["n"]["enum"].append(3)
        value.input_schema["required"].append("other")
        exposed = dict(value.input_schema)
        exposed["default"]["items"][0]["value"] = 7
        self.assertEqual(dict(value.input_schema), expected)
        self.assertEqual(dict(deepcopy(value).input_schema), expected)
        with self.assertRaises(TypeError):
            value.input_schema["type"] = "string"

    def test_schema_must_be_finite_json_without_enforcing_schema_support(self):
        for bad in (object(), float("nan"), {1: "bad-key"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ToolDefinition("echo", "Echo", {"type": "object", "default": bad})
        value = ToolDefinition("echo", "Echo", {"type": "object", "unknown": True})
        self.assertTrue(value.input_schema["unknown"])


class RuntimeDefinitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_capture_drives_provider_validation_approval_gate_and_execution(self):
        initial = definition(requires_approval=True, side_effects="read")
        executed, gated = [], []

        async def execute(arguments, context):
            executed.append((arguments, context.definition))
            return ToolOutput(str(arguments["n"]))

        class Gate(DeclaredSideEffectGate):
            async def execute(self, tool, arguments, context):
                gated.append(tool.definition)
                return await super().execute(tool, arguments, context)

        class MutatingProvider(FakeProvider):
            async def stream(self, messages, tools=(), *, context):
                tools[0].input_schema["properties"]["n"]["type"] = "string"
                tools[0].input_schema["required"].clear()
                async for event in super().stream(messages, tools, context=context):
                    yield event

        tool = MutableTool(initial, execute)
        policy = StaticApprovalPolicy(True)
        provider = MutatingProvider([
            [ToolCall("bad", "echo", {"n": "1"}), ToolCall("good", "echo", {"n": 1}), Finish("tool_calls")],
            [ToolCall("second", "echo", {"n": 2}), Finish("tool_calls")], [Finish()],
        ])
        components = AgentComponents(provider, tools=(tool,), approval_policy=policy,
                                     execution_gate=Gate(allowed_side_effects=("read",)))
        tool.reads = 0
        agent = Agent(components)
        tool.current = replace(initial, description="Changed", requires_approval=False,
                               side_effects="none", timeout_seconds=0,
                               input_schema={"type": "object", "properties": {"n": {"type": "string"}}})
        result = await agent.run("q")
        self.assertEqual(tool.reads, 1)
        self.assertEqual(executed, [({"n": 1}, initial), ({"n": 2}, initial)])
        self.assertEqual(gated, [initial, initial])
        self.assertEqual([c.definition for c in policy.requests], [initial, initial])
        self.assertTrue(result.messages[2].is_error)
        self.assertEqual([tools[0] for tools in provider.requested_tools], [initial] * 3)

    async def test_effect_cannot_be_relaxed_after_capture(self):
        executed = []
        async def execute(arguments, context):
            executed.append(arguments)
            return ToolOutput("unexpected")
        tool = MutableTool(definition(side_effects="read"), execute)
        agent = Agent(AgentComponents(FakeProvider([
            [ToolCall("c", "echo", {"n": 1}), Finish("tool_calls")], [Finish()],
        ]), tools=(tool,)))
        tool.current = replace(tool.current, side_effects="none")
        result = await agent.run("q")
        self.assertEqual(executed, [])
        self.assertTrue(result.messages[2].is_error)
        self.assertIn("blocked", result.messages[2].content)

    async def test_timeout_cannot_be_removed_after_capture(self):
        resumed = []
        async def execute(arguments, context):
            await asyncio.sleep(0)
            resumed.append(True)
            return ToolOutput("unexpected")
        tool = MutableTool(definition(timeout_seconds=0), execute)
        agent = Agent(AgentComponents(FakeProvider([
            [ToolCall("c", "echo", {"n": 1}), Finish("tool_calls")], [Finish()],
        ]), tools=(tool,)))
        tool.current = replace(tool.current, timeout_seconds=None)
        result = await agent.run("q")
        self.assertEqual(resumed, [])
        self.assertIn("timeout", result.messages[2].content)

    async def test_old_agent_keeps_name_and_new_agent_adopts_new_definition(self):
        async def execute(arguments, context):
            return ToolOutput(context.definition.name)
        tool = MutableTool(definition(), execute)
        old = Agent(AgentComponents(FakeProvider([
            [ToolCall("c", "echo", {"n": 1}), Finish("tool_calls")], [Finish()],
        ]), tools=(tool,)))
        tool.current = replace(tool.current, name="renamed")
        new = Agent(AgentComponents(FakeProvider([
            [ToolCall("c", "renamed", {"n": 1}), Finish("tool_calls")], [Finish()],
        ]), tools=(tool,)))
        self.assertEqual((await old.run("q")).messages[2].content, "echo")
        self.assertEqual((await new.run("q")).messages[2].content, "renamed")

    async def test_cancellation_reaches_original_handler_after_capture(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def execute(arguments, context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        tool = MutableTool(definition(), execute)
        agent = Agent(AgentComponents(FakeProvider([
            [ToolCall("c", "echo", {"n": 1}), Finish("tool_calls")],
        ]), tools=(tool,)))
        task = asyncio.create_task(agent.run("q"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(m.role == "tool" for m in agent.messages))

    def test_duplicate_names_are_rechecked_at_agent_capture(self):
        async def execute(arguments, context):
            return ToolOutput("ok")
        first = MutableTool(definition(), execute)
        second = MutableTool(replace(definition(), name="other"), execute)
        components = AgentComponents(FakeProvider([]), tools=(first, second))
        second.current = definition()
        with self.assertRaisesRegex(ValueError, "duplicate tool names: echo"):
            Agent(components)
