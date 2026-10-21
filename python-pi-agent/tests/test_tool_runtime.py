from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from pi_agent_py import (
    Agent,
    AgentLoopLimitError,
    ApprovalDecision,
    DeclaredSideEffectSandbox,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    JSONObject,
    Message,
    StaticApprovalPolicy,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)


class RecordingNextTurnHook:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Message, ...], int]] = []

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        next_request_number: int,
    ) -> Sequence[Message]:
        history = tuple(messages)
        self.calls.append((history, next_request_number))
        return history


class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_approves_executes_and_returns_result_to_next_model_request(
        self,
    ) -> None:
        executions: list[dict[str, object]] = []
        execution_contexts: list[ToolExecutionContext] = []

        async def add(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            executions.append(arguments)
            execution_contexts.append(context)
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
            session_id="session-1",
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
        approval_context = policy.requests[0]
        self.assertEqual(approval_context.session_id, "session-1")
        self.assertEqual(approval_context.request_number, 1)
        self.assertEqual(approval_context.call_index, 1)
        self.assertIsNone(approval_context.approval)
        self.assertEqual(execution_contexts[0].call, approval_context.call)
        self.assertTrue(execution_contexts[0].approval.approved)
        self.assertEqual(
            [
                event.kind
                for event in events.events
                if event.channel == "capability"
            ],
            [
                "context_prepared",
                "tool_call_received",
                "tool_approval_requested",
                "tool_approval_decided",
                "tool_execution_started",
                "tool_execution_finished",
                "context_prepared",
            ],
        )

    async def test_next_turn_hook_runs_after_tool_result_commit(self) -> None:
        async def add(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
            return ToolOutput(content="2")

        provider = FakeProvider(
            [
                [
                    ToolCall("call-hook", "add", {"a": 1, "b": 1}),
                    Finish("tool_calls"),
                ],
                [TextDelta("done"), Finish()],
            ]
        )
        hook = RecordingNextTurnHook()
        agent = Agent(
            provider,
            tools=[FunctionTool(_add_definition(), add)],
            next_turn_hook=hook,
        )

        await agent.run("use a tool")

        self.assertEqual(len(hook.calls), 1)
        prepared_history, next_request_number = hook.calls[0]
        self.assertEqual(next_request_number, 2)
        self.assertEqual(
            [message.role for message in prepared_history],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(provider.requests[1], prepared_history)

    async def test_invalid_arguments_are_returned_without_executing_tool(self) -> None:
        executed = False

        async def must_not_run(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del context
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

        async def must_not_run(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del context
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
        async def never_finishes(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
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

        async def blocking_tool(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
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
        async def add(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
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

    async def test_default_sandbox_denies_declared_read_effect(self) -> None:
        executed = False

        async def read_data(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            nonlocal executed
            del arguments, context
            executed = True
            return ToolOutput(content="private data")

        definition = ToolDefinition(
            name="read_data",
            description="Read protected data.",
            input_schema={"type": "object", "properties": {}},
            side_effects="read",
        )
        provider = FakeProvider(
            [
                [ToolCall("call-read", "read_data", {}), Finish("tool_calls")],
                [TextDelta("read was blocked"), Finish()],
            ]
        )
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            tools=[FunctionTool(definition, read_data)],
            event_sink=events,
        )

        result = await agent.run("read data")

        self.assertFalse(executed)
        self.assertTrue(result.messages[2].is_error)
        self.assertIn("blocked by the sandbox", result.messages[2].content)
        denied = [
            event for event in events.events if event.kind == "tool_sandbox_denied"
        ]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].payload["side_effects"], "read")
        finished = [
            event
            for event in events.events
            if event.kind == "tool_execution_finished"
        ]
        self.assertEqual(finished[0].payload["outcome"], "sandbox_denied")

    async def test_configured_sandbox_allows_only_declared_scope(self) -> None:
        contexts: list[ToolExecutionContext] = []

        async def read_data(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            arguments["handler_mutation"] = True
            contexts.append(context)
            return ToolOutput(content="allowed data")

        definition = ToolDefinition(
            name="read_data",
            description="Read approved data.",
            input_schema={"type": "object", "properties": {}},
            side_effects="read",
        )
        provider = FakeProvider(
            [
                [ToolCall("call-read", "read_data", {}), Finish("tool_calls")],
                [TextDelta("done"), Finish()],
            ]
        )
        sandbox = DeclaredSideEffectSandbox(
            allowed_side_effects=("none", "read"),
            allowed_tools=("read_data",),
        )
        agent = Agent(
            provider,
            tools=[FunctionTool(definition, read_data)],
            sandbox=sandbox,
        )

        result = await agent.run("read data")

        self.assertEqual(result.messages[2].content, "allowed data")
        self.assertEqual(contexts[0].call.id, "call-read")
        self.assertEqual(contexts[0].definition.side_effects, "read")
        self.assertNotIn(
            "handler_mutation",
            result.messages[1].tool_calls[0].arguments,
        )

    async def test_approval_snapshot_cannot_replace_execution_arguments(self) -> None:
        executed_arguments: list[JSONObject] = []

        class MutatingApprovalPolicy:
            async def review(
                self,
                context: ToolExecutionContext,
            ) -> ApprovalDecision:
                context.call.arguments["a"] = 999
                return ApprovalDecision(True, "approved original request")

        async def add(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            executed_arguments.append(dict(arguments))
            self.assertEqual(context.call.arguments["a"], 1)
            return ToolOutput(content="2")

        provider = FakeProvider(
            [
                [ToolCall("call-safe", "add", {"a": 1, "b": 1}), Finish("tool_calls")],
                [TextDelta("done"), Finish()],
            ]
        )
        agent = Agent(
            provider,
            tools=[FunctionTool(_add_definition(requires_approval=True), add)],
            approval_policy=MutatingApprovalPolicy(),
        )

        result = await agent.run("add")

        self.assertEqual(executed_arguments, [{"a": 1, "b": 1}])
        self.assertEqual(
            result.messages[1].tool_calls[0].arguments,
            {"a": 1, "b": 1},
        )

    async def test_approval_policy_failure_is_closed_and_observable(self) -> None:
        executed = False

        class ExplodingApprovalPolicy:
            async def review(
                self,
                context: ToolExecutionContext,
            ) -> ApprovalDecision:
                del context
                raise RuntimeError("approval backend unavailable")

        async def must_not_run(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            nonlocal executed
            del arguments, context
            executed = True
            return ToolOutput(content="unexpected")

        provider = FakeProvider(
            [
                [ToolCall("call-denied", "add", {"a": 1, "b": 1}), Finish("tool_calls")],
                [TextDelta("denied"), Finish()],
            ]
        )
        events = InMemoryEventSink()
        agent = Agent(
            provider,
            tools=[
                FunctionTool(
                    _add_definition(requires_approval=True),
                    must_not_run,
                )
            ],
            approval_policy=ExplodingApprovalPolicy(),
            event_sink=events,
        )

        result = await agent.run("add")

        self.assertFalse(executed)
        self.assertTrue(result.messages[2].is_error)
        decisions = [
            event
            for event in events.events
            if event.kind == "tool_approval_decided"
        ]
        self.assertEqual(decisions[0].payload["approved"], False)
        self.assertEqual(decisions[0].payload["policy_error"], "RuntimeError")


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
