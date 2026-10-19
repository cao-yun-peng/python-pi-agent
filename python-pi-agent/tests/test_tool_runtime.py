from __future__ import annotations

import asyncio
import unittest

from pi_agent_py import (
    Agent,
    AgentLoopLimitError,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    JSONObject,
    StaticApprovalPolicy,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolOutput,
)


class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_approves_executes_and_returns_result_to_next_model_request(
        self,
    ) -> None:
        executions: list[dict[str, object]] = []

        async def add(arguments: JSONObject) -> ToolOutput:
            executions.append(arguments)
            left = arguments["a"]
            right = arguments["b"]
            assert isinstance(left, int) and not isinstance(left, bool)
            assert isinstance(right, int) and not isinstance(right, bool)
            value = left + right
            return ToolOutput(
                content=str(value),
                structured_content={"value": value},
            )

        tool = FunctionTool(_add_definition(requires_approval=True), add)
        policy = StaticApprovalPolicy(True, reason="test grants access")
        provider = FakeProvider(
            [
                [
                    ToolCall("call-1", "add", {"a": 2, "b": 3}),
                    Finish("tool_calls"),
                    TokenUsage(10, 2, 12, cached_input_tokens=1),
                ],
                [
                    TextDelta("结果是 5。"),
                    Finish("stop"),
                    TokenUsage(15, 4, 19, cached_input_tokens=2),
                ],
            ]
        )
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            tools=[tool],
            approval_policy=policy,
            event_sink=events,
        )

        result = await agent.run("计算 2 + 3")

        self.assertEqual(result.message.content, "结果是 5。")
        self.assertEqual(result.usage, TokenUsage(25, 6, 31, cached_input_tokens=3))
        self.assertEqual(executions, [{"a": 2, "b": 3}])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requested_tools[0], (tool.definition,))
        self.assertEqual(
            [message.role for message in result.messages],
            ["user", "assistant", "tool", "assistant"],
        )
        tool_message = result.messages[2]
        self.assertEqual(tool_message.tool_call_id, "call-1")
        self.assertEqual(tool_message.content, "5")
        self.assertEqual(tool_message.structured_content, {"value": 5})
        self.assertFalse(tool_message.is_error)
        self.assertEqual(len(policy.requests), 1)
        self.assertEqual(
            [
                event.kind
                for event in events.events
                if event.channel == "capability"
            ],
            [
                "tool_call_received",
                "tool_approval_requested",
                "tool_approval_decided",
                "tool_execution_started",
                "tool_execution_finished",
            ],
        )

    async def test_invalid_arguments_are_returned_without_executing_tool(self) -> None:
        executed = False

        async def must_not_run(arguments: JSONObject) -> ToolOutput:
            nonlocal executed
            executed = True
            return ToolOutput(content=str(arguments))

        provider = FakeProvider(
            [
                [ToolCall("call-2", "add", {"a": "two"}), Finish("tool_calls")],
                [TextDelta("参数不正确。"), Finish()],
            ]
        )
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            tools=[FunctionTool(_add_definition(), must_not_run)],
            event_sink=events,
        )

        result = await agent.run("错误参数")

        self.assertFalse(executed)
        tool_message = result.messages[2]
        self.assertTrue(tool_message.is_error)
        self.assertIn("$.a: expected integer", tool_message.content)
        self.assertIn("$.b: required property is missing", tool_message.content)
        self.assertIn("tool_validation_failed", [event.kind for event in events.events])

    async def test_denied_approval_becomes_model_visible_tool_error(self) -> None:
        executed = False

        async def must_not_run(arguments: JSONObject) -> ToolOutput:
            nonlocal executed
            executed = True
            return ToolOutput(content=str(arguments))

        provider = FakeProvider(
            [
                [
                    ToolCall("call-3", "add", {"a": 1, "b": 1}),
                    Finish("tool_calls"),
                ],
                [TextDelta("操作没有获批。"), Finish()],
            ]
        )
        agent = Agent(
            provider,
            tools=[FunctionTool(_add_definition(requires_approval=True), must_not_run)],
        )

        result = await agent.run("需要审批")

        self.assertFalse(executed)
        self.assertTrue(result.messages[2].is_error)
        self.assertIn("denied", result.messages[2].content)

    async def test_zero_timeout_is_deterministic_and_returns_tool_error(self) -> None:
        async def never_finishes(arguments: JSONObject) -> ToolOutput:
            del arguments
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        definition = ToolDefinition(
            name="wait",
            description="Wait forever for timeout testing.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            timeout_seconds=0,
        )
        provider = FakeProvider(
            [
                [ToolCall("call-4", "wait", {}), Finish("tool_calls")],
                [TextDelta("工具超时。"), Finish()],
            ]
        )
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            tools=[FunctionTool(definition, never_finishes)],
            event_sink=events,
        )

        result = await agent.run("测试超时")

        self.assertTrue(result.messages[2].is_error)
        self.assertIn("timeout", result.messages[2].content)
        finished = [
            event
            for event in events.events
            if event.kind == "tool_execution_finished"
        ]
        self.assertEqual(finished[0].payload["outcome"], "timeout")

    async def test_cancellation_propagates_into_running_tool(self) -> None:
        started = asyncio.Event()

        async def blocking_tool(arguments: JSONObject) -> ToolOutput:
            del arguments
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        definition = ToolDefinition(
            name="block",
            description="Block until the owning agent task is cancelled.",
            input_schema={"type": "object", "properties": {}},
        )
        provider = FakeProvider(
            [[ToolCall("call-5", "block", {}), Finish("tool_calls")]]
        )
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            tools=[FunctionTool(definition, blocking_tool)],
            event_sink=events,
        )

        task = asyncio.create_task(agent.run("取消工具"))
        await started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual([message.role for message in agent.messages], ["user", "assistant"])
        self.assertEqual(
            [event.kind for event in events.events[-3:]],
            ["tool_execution_finished", "run_cancelled", "run_settled"],
        )
        self.assertEqual(events.events[-3].payload["outcome"], "cancelled")

    async def test_unknown_tool_becomes_error_and_model_can_recover(self) -> None:
        provider = FakeProvider(
            [
                [ToolCall("call-6", "missing", {}), Finish("tool_calls")],
                [TextDelta("该工具不可用。"), Finish()],
            ]
        )
        agent = Agent(provider)

        result = await agent.run("调用不存在的工具")

        self.assertTrue(result.messages[2].is_error)
        self.assertIn("not available", result.messages[2].content)

    async def test_model_request_limit_stops_infinite_tool_loop(self) -> None:
        async def add(arguments: JSONObject) -> ToolOutput:
            del arguments
            return ToolOutput(content="2")

        provider = FakeProvider(
            [[ToolCall("call-7", "add", {"a": 1, "b": 1}), Finish("tool_calls")]]
        )
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            tools=[FunctionTool(_add_definition(), add)],
            event_sink=events,
            max_model_requests=1,
        )

        with self.assertRaisesRegex(AgentLoopLimitError, "exceeded 1"):
            await agent.run("持续调用工具")

        self.assertEqual(
            [event.kind for event in events.events[-2:]],
            ["run_failed", "run_settled"],
        )


def _add_definition(*, requires_approval: bool = False) -> ToolDefinition:
    return ToolDefinition(
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
        side_effects="none",
        requires_approval=requires_approval,
        retry_safe=True,
        replay_safe=True,
    )


if __name__ == "__main__":
    unittest.main()
