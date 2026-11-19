from __future__ import annotations

import asyncio
import unittest
from typing import Annotated, Any, Literal, Optional

from pi_agent_py.api import (
    Agent, AgentComponents, DecoratedTool, Finish, InMemoryEventSink, JSONObject,
    ToolCall, ToolError, ToolExecutionContext, ToolOutput, ToolValidationError, tool,
)
from pi_agent_py.testing import FakeProvider
from pi_agent_py.tools.validation import validate_tool_arguments


def context_for(adapted: DecoratedTool) -> ToolExecutionContext:
    return ToolExecutionContext(
        call=ToolCall("c", adapted.definition.name, {}), definition=adapted.definition,
        request_number=1, call_index=1, session_id="demo",
    )


class DecoratorTests(unittest.IsolatedAsyncioTestCase):
    def test_nested_jsonobject_alias_without_deferred_annotations(self) -> None:
        for annotation, expected in (
            (Optional[JSONObject], {"type": ["object", "null"]}),
            (list[JSONObject], {"type": "array", "items": {"type": "object"}}),
            (Annotated[JSONObject, "JSON data"], {"type": "object", "description": "JSON data"}),
        ):
            async def example(value):
                """Accept nested JSON object aliases."""
            example.__annotations__ = {"value": annotation}
            with self.subTest(annotation=annotation):
                self.assertEqual(tool(example).definition.input_schema["properties"]["value"], expected)

    def test_schema_mapping_table(self) -> None:
        cases = [
            (str, {"type": "string"}), (int, {"type": "integer"}),
            (float, {"type": "number"}), (bool, {"type": "boolean"}),
            (Literal["a", "b"], {"type": "string", "enum": ["a", "b"]}),
            (Literal[1, 2], {"type": "integer", "enum": [1, 2]}),
            (Literal[True, False], {"type": "boolean", "enum": [True, False]}),
            (list[int], {"type": "array", "items": {"type": "integer"}}),
            (dict[str, object], {"type": "object"}), (dict[str, Any], {"type": "object"}),
            (JSONObject, {"type": "object"}),
            (Optional[list[int]], {"type": ["array", "null"], "items": {"type": "integer"}}),
            (Literal["a"] | None, {"type": ["string", "null"], "enum": ["a", None]}),
            (Annotated[str, "Path"], {"type": "string", "description": "Path"}),
        ]
        for annotation, expected in cases:
            async def example(value):
                """Example."""
            example.__annotations__ = {"value": annotation}
            with self.subTest(annotation=annotation):
                self.assertEqual(tool(example).definition.input_schema, {
                    "type": "object", "properties": {"value": expected},
                    "required": ["value"], "additionalProperties": False,
                })

    async def test_defaults_context_and_float_binding(self) -> None:
        seen = []
        @tool
        async def example(a: float, context: ToolExecutionContext, b: int = 3) -> dict[str, object]:
            """First paragraph.

            Other documentation.
            """
            seen.append((type(a), context))
            return {"b": b, "a": a}
        context = context_for(example)
        output = await example.execute({"a": 2}, context)
        self.assertEqual(seen, [(float, context)])
        self.assertEqual(output.content, '{"a": 2.0, "b": 3}')
        self.assertEqual(example.definition.description, "First paragraph.")
        self.assertEqual(example.definition.input_schema["required"], ["a"])
        self.assertEqual(example.definition.input_schema["properties"]["b"]["default"], 3)
        self.assertIs(example.fn, example.__wrapped__)
        self.assertEqual(await example.fn(2, context), {"a": 2, "b": 3})
        other = tool(example.fn, name="renamed", description="Override")
        self.assertIsNot(other, example)
        self.assertEqual(other.definition.name, "renamed")
        with self.assertRaises(TypeError):
            await example.execute({}, context)

    def test_rejects_invalid_signatures_and_metadata(self) -> None:
        invalid = [set[int], tuple[int], bytes, list, dict, dict[int, str],
                   int | str, Literal[1, "a"], object, None]
        for annotation in invalid:
            async def example(value):
                """Example."""
            example.__annotations__ = {} if annotation is None else {"value": annotation}
            with self.subTest(annotation=annotation), self.assertRaisesRegex(TypeError, "example"):
                tool(example)
        def synchronous():
            """Sync."""
        async def generator():
            """Generator."""
            yield 1
        async def positional(a: str, /):
            """Positional."""
        async def variadic(*a: str):
            """Variadic."""
        async def keywords(**a: str):
            """Keywords."""
        async def contexts(a: ToolExecutionContext, b: ToolExecutionContext):
            """Contexts."""
        async def defaults(a: str = object()):
            """Defaults."""
        async def unresolved(a: str):
            """Unresolved."""
        unresolved.__annotations__["a"] = "MissingType"
        for fn in (synchronous, generator, positional, variadic, keywords, contexts, defaults, unresolved):
            with self.subTest(fn=fn.__name__), self.assertRaisesRegex(TypeError, fn.__name__):
                tool(fn)
        async def empty():
            pass
        for options in ({}, {"name": "bad.name"}, {"name": ""}, {"description": " "}):
            with self.assertRaises(ValueError):
                tool(empty, **options)

    async def test_explicit_error_and_internal_error_through_agent(self) -> None:
        for explicit in (True, False):
            @tool(description="Fail deliberately")
            async def failure():
                if explicit:
                    raise ToolError("safe error", structured_content={"status": "missing"})
                raise RuntimeError("private-secret")
            provider = FakeProvider([[ToolCall("c", "failure", {}), Finish("tool_calls")], [Finish()]])
            result = await Agent(AgentComponents(provider, tools=(failure,))).run("test")
            output = result.messages[2]
            self.assertTrue(output.is_error)
            self.assertNotIn("private-secret", output.content)
            self.assertEqual(output.content, "safe error" if explicit else "Tool execution failed (RuntimeError).")

    def test_optional_nested_validation_and_required_order(self) -> None:
        @tool
        async def example(first: list[int] | None, second: bool, count: int = 2):
            """Validate nested lists."""
        schema = example.definition.input_schema
        self.assertEqual(schema["required"], ["first", "second"])
        for value in (None, [], [1, 2]):
            validate_tool_arguments(schema, {"first": value, "second": True})
        for value in ("bad", ["bad"], [True]):
            with self.assertRaises(ToolValidationError):
                validate_tool_arguments(schema, {"first": value, "second": True})

    def test_independent_decorations_and_default_snapshot(self) -> None:
        default = [1]
        async def example(items: list[int] = default):
            """Default list."""
        first, second = tool(example), tool(example)
        self.assertIsNot(first, second)
        default.append(2)
        self.assertEqual(first.definition.input_schema["properties"]["items"]["default"], [1])
        with self.assertRaisesRegex(ValueError, "duplicate tool names"):
            AgentComponents(FakeProvider([]), tools=(first, second))

    async def test_return_mapping_and_explicit_error(self) -> None:
        for value in ("text", None, {"b": 2, "a": 1}, [1], 1, 2.5, True, ToolOutput("ready")):
            @tool(description="Return a value")
            async def example():
                return value
            output = await example.execute({}, context_for(example))
            if isinstance(value, ToolOutput):
                self.assertIs(output, value)
            elif value is None:
                self.assertEqual(output, ToolOutput(""))
            elif isinstance(value, str):
                self.assertEqual(output, ToolOutput(value))
            else:
                self.assertEqual(output.structured_content, value)
        for value in (object(), (1,), {"nested": object()}, float("nan")):
            @tool(description="Bad return")
            async def example():
                return value
            with self.assertRaises((TypeError, ValueError)):
                await example.execute({}, context_for(example))
        @tool
        async def failure():
            """An explicit model error."""
            raise ToolError("safe error", structured_content={"reason": "missing"})
        self.assertEqual(await failure.execute({}, context_for(failure)),
                         ToolOutput("safe error", {"reason": "missing"}, is_error=True))
        @tool
        async def crash():
            """Internal error."""
            raise RuntimeError("secret")
        with self.assertRaisesRegex(RuntimeError, "secret"):
            await crash.execute({}, context_for(crash))

    async def test_runtime_validation_approval_gate_timeout_and_success(self) -> None:
        called = []
        async def example(a: int) -> str:
            """Example."""
            called.append(a)
            await asyncio.sleep(0)
            return str(a)
        for options, arguments, event in [
            ({}, {"a": 1}, "tool_execution_finished"),
            ({}, {"a": "bad"}, "tool_validation_failed"),
            ({"requires_approval": True}, {"a": 1}, "tool_approval_decided"),
            ({"side_effects": "write"}, {"a": 1}, "tool_execution_denied"),
            ({"timeout_seconds": 0}, {"a": 1}, "tool_execution_finished"),
        ]:
            with self.subTest(options=options, arguments=arguments):
                called.clear()
                adapted = tool(example, **options)
                provider = FakeProvider([[ToolCall("c", "example", arguments), Finish("tool_calls")], [Finish()]])
                events = InMemoryEventSink()
                result = await Agent(AgentComponents(provider, tools=(adapted,)), event_sink=events).run("go")
                self.assertIn(event, [e.kind for e in events.events])
                output = result.messages[2]
                self.assertEqual(output.is_error, bool(options) or arguments["a"] == "bad")
                if not options and arguments["a"] == 1:
                    self.assertEqual(output.content, "1")
                elif event != "tool_execution_finished":
                    self.assertEqual(called, [])

    async def test_cancel_reaches_handler(self) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        @tool
        async def waiting():
            """Wait until cancelled."""
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        events = InMemoryEventSink()
        agent = Agent(AgentComponents(FakeProvider([[ToolCall("c", "waiting", {}), Finish("tool_calls")]]), tools=(waiting,)), event_sink=events)
        task = asyncio.create_task(agent.run("go"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertIn("run_cancelled", [e.kind for e in events.events])


class TypeListValidationTests(unittest.TestCase):
    def test_type_list_accepts_members_and_validates_nested_values(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": ["array", "null"], "items": {"type": "integer"}}}}
        for value in (None, [1, 2]):
            validate_tool_arguments(schema, {"x": value})
        for value in ("bad", ["bad"]):
            with self.assertRaises(ToolValidationError):
                validate_tool_arguments(schema, {"x": value})

    def test_invalid_type_lists_fail(self) -> None:
        for types in ([], ["string", 1]):
            with self.assertRaisesRegex(ToolValidationError, "invalid schema type list"):
                validate_tool_arguments({"type": types}, {})
